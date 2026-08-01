#!/usr/bin/env python3
"""Run the test suite across several processes.

    python3 tests/run.py              # every test, in parallel
    python3 tests/run.py -j 1         # one process, for a clean traceback
    python3 tests/run.py tests.test_tui.NamingChannels

Most of the suite drives the panel through Textual's `Pilot`, and nearly all
of that time is spent waiting rather than computing.  `Pilot` confirms a
keypress by sleeping in 20 ms steps until the process stops using CPU, so
one simulated key costs about a tenth of a second however fast the machine
is, and a panel test that presses half a dozen keys costs a second and a
half of a mostly idle core.  Serially that is two minutes; spread over the
cores that are sitting there it is twenty-odd seconds.

One test per unit of work, handed out as workers free up.  More workers than
cores does not help -- `Pilot`'s idle check is measuring CPU time, so a
loaded machine makes every test look busy for longer -- which is why the
default is `os.cpu_count()` and not some multiple of it.

`python3 -m unittest discover -s tests -t .` still works and is still the
last word: this only changes how the same tests are scheduled.  Nothing here
is imported by the tests themselves.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import unittest
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def paths_under(names: list[str]) -> list[str]:
    """Every test under `names`, one dotted path each.

    One test per unit of work rather than one class: the panel tests are not
    the same length as each other, and a class held back to the end of the
    run decides the wall clock on its own.  Nothing here has a `setUpClass`
    to pay for twice.
    """
    loader = unittest.TestLoader()
    suite = (loader.loadTestsFromNames(names) if names
             else loader.discover(os.path.join(ROOT, "tests"),
                                  top_level_dir=ROOT))
    return [f"{type(t).__module__}.{type(t).__qualname__}.{t._testMethodName}"
            for t in iter_tests(suite)]


def iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


def run_one(path: str) -> tuple[str, int, int, float, str]:
    """Run one test in this process and report what happened.

    Output is captured and handed back rather than written: several processes
    printing to one terminal interleave into something nobody can read.  Only
    the tracebacks come back -- unittest's own per-run summary would be one
    "Ran 1 test" block per test, which buries them.
    """
    import io

    start = time.monotonic()
    suite = unittest.TestLoader().loadTestsFromName(path)
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    bad = result.failures + result.errors
    detail = "".join(f"{'=' * 70}\n{kind}: {test}\n{'-' * 70}\n{trace}"
                     for kind, (test, trace)
                     in zip(["FAIL"] * len(result.failures)
                            + ["ERROR"] * len(result.errors), bad))
    return path, result.testsRun, len(bad), time.monotonic() - start, detail


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("names", nargs="*", help="dotted test names (default: all)")
    p.add_argument("-j", "--jobs", type=int, default=0,
                   help="worker processes (default: one per CPU)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="list every test by how long it took")
    args = p.parse_args(argv)

    paths = paths_under(args.names)
    if not paths:
        print("no tests found", file=sys.stderr)
        return 2
    jobs = args.jobs or min(len(paths), os.cpu_count() or 4)

    start = time.monotonic()
    total = failed = 0
    reports: list[str] = []
    slowest: list[tuple[float, str]] = []

    def collect(results) -> None:
        nonlocal total, failed
        for path, ran, bad, took, detail in results:
            total += ran
            failed += bad
            slowest.append((took, path))
            sys.stdout.write("F" if bad else ".")
            sys.stdout.flush()
            if detail:
                reports.append(detail)

    if jobs == 1:
        collect(map(run_one, paths))
    else:
        # One test per task, handed out as workers free up, so a slow one
        # cannot leave seven processes idle behind it.
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            collect(pool.map(run_one, paths, chunksize=1))
    print()

    for detail in reports:
        print(detail)
    if args.verbose:
        for took, path in sorted(slowest, reverse=True):
            print(f"  {took:6.2f}s  {path}")

    elapsed = time.monotonic() - start
    where = "1 process" if jobs == 1 else f"{jobs} processes"
    print(f"Ran {total} tests in {elapsed:.1f}s across {where}")
    print("FAILED" if failed else "OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
