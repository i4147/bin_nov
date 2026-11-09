#!/data/data/com.termux/files/usr/bin/env python
"""Run a program repeatedly until it fails.
Executes the command in a loop (optionally ``-p`` in parallel), stops at the first exit status outside the accepted codes, sleeps between the iterations and reports the failing output.
Usage: failtest [--code 0] [-s SECONDS] [-p N] COMMAND ...
Positional arguments: command Command to run Options: --code Allowed exit codes [DEFAULT: 0] -s, --sleep Sleep between iterations [DEFAULT: %(default)s] -p, --parallel Number of parallel processes [DEFAULT: %(default)s] Examples: $ failtest -s 0.1 -p 4 pytest -x tests Command-line script: run ``python failtest.py`` or the ``failtest`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import subprocess
import sys
import time
from typing import Optional, Sequence


def run_command(command: str, allowed_codes: tuple[int, ...], sleep: float, iteration: int) -> tuple[int, int]:
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _stdout, _stderr = process.communicate()
    print(f"Iteration {iteration}")
    if process.returncode not in allowed_codes:
        return process.returncode, iteration
    time.sleep(sleep)
    return 0, iteration


def main(args: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="run a program until it fails")
    parser.add_argument("command", type=str, help="command to run")
    parser.add_argument(
        "--code",
        dest="codes",
        default=(0,),
        nargs="+",
        type=int,
        help="allowed exit codes [DEFAULT: 0]",
    )
    parser.add_argument(
        "-s",
        "--sleep",
        dest="sleep",
        type=float,
        default=1.0,
        help="sleep between iterations [DEFAULT: %(default)s]",
    )
    parser.add_argument(
        "-p",
        "--parallel",
        dest="parallel",
        type=int,
        default=1,
        help="number of parallel processes [DEFAULT: %(default)s]",
    )
    options = parser.parse_args(args)
    allowed_codes: tuple[int, ...] = tuple(options.codes)
    ctr: int = 0
    start_loop: float = time.time()
    try:
        if options.parallel <= 1:
            while True:
                ctr += 1
                returncode, _ = run_command(options.command, allowed_codes, options.sleep, ctr)
                if returncode != 0:
                    return returncode
        else:
            with ProcessPoolExecutor(max_workers=options.parallel) as executor:
                futures = {}
                for i in range(options.parallel):
                    ctr += 1
                    future = executor.submit(run_command, options.command, allowed_codes, options.sleep, ctr)
                    futures[future] = ctr
                while futures:
                    for future in as_completed(futures):
                        iteration = futures.pop(future)
                        returncode, _ = future.result()
                        if returncode != 0:
                            for pending in futures:
                                pending.cancel()
                            return returncode
                        ctr += 1
                        new_future = executor.submit(run_command, options.command, allowed_codes, options.sleep, ctr)
                        futures[new_future] = ctr
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
