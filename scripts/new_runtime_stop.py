"""Retired operational entry point.

Qualification and release control are maintained in the deployment owner's
private operations workflow. This public application source performs no host
or service operations.
"""


def main():
    raise SystemExit("This entry point is retired; use the reviewed private operations workflow.")


if __name__ == "__main__":
    main()
