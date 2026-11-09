#!/data/data/com.termux/files/usr/bin/env python
"""Run ``srp`` once for every ASCII letter.
Calls the external ``srp`` command with each lowercase and then each uppercase letter, printing the output of each run.
Usage: python srpr.py Examples: $ python srpr.py Wrapper around the external ``srp`` executable; it does nothing useful without it."""

from dh import runcmd


if __name__ == "__main__":
    from string import ascii_lowercase, ascii_uppercase

    for char in ascii_lowercase:
        cmd = ["srp", char]
        runcmd(cmd, show_output=True)
    for char in ascii_uppercase:
        cmd = ["srp", char]
        runcmd(cmd, show_output=True)
    print("done")
