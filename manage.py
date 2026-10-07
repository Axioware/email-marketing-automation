#!/usr/bin/env python
"""Django's command-line utility: runs the server, the pipeline commands and migrations."""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "emailautomation.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
