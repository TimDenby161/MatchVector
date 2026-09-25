"""Validation helpers for GitHub Actions workflow inputs."""
import argparse
import re

INT_LIST_RE = re.compile(r"^[0-9]+(?:[ \t]+[0-9]+)*$")


def parse_int_list(value, name):
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{name} must contain at least one integer")
    if not INT_LIST_RE.fullmatch(value):
        raise ValueError(f"{name} must be a space-separated list of integers")
    return [int(part) for part in value.split()]


def bash_array(name, values):
    return f"{name}=({' '.join(str(value) for value in values)})"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate workflow integer-list inputs")
    parser.add_argument("leagues")
    parser.add_argument("seasons")
    args = parser.parse_args(argv)

    try:
        leagues = parse_int_list(args.leagues, "leagues")
        seasons = parse_int_list(args.seasons, "seasons")
    except ValueError as exc:
        parser.error(str(exc))
    print(bash_array("LEAGUE_ARGS", leagues))
    print(bash_array("SEASON_ARGS", seasons))


if __name__ == "__main__":
    main()
