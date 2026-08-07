"""CLI wiring smoke tests: every command is registered and importable."""

import pytest
from click.testing import CliRunner

from mytube.cli import main


@pytest.mark.parametrize(
    "args",
    [
        ["--help"],
        ["db", "--help"],
        ["db", "migrate", "--help"],
        ["auth", "--help"],
        ["takeout", "--help"],
        ["takeout", "import", "--help"],
        ["subs", "--help"],
        ["candidates", "--help"],
        ["candidates", "pull-rss", "--help"],
        ["hydrate", "--help"],
        ["taste", "--help"],
        ["embed", "--help"],
        ["benchmark", "--help"],
        ["benchmark", "freeze", "--help"],
        ["benchmark", "run", "--help"],
        ["benchmark", "sheet", "--help"],
        ["benchmark", "verdict", "--help"],
        ["status", "--help"],
    ],
)
def test_command_help(args):
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
