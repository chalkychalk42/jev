"""The hive gives a step the live bot's attempts (V341)."""

from jev.run import cli


def test_the_hive_gives_a_step_the_live_bots_attempts():
    """V341: the hive's one attempt a step made its first unreachable walk to a quest giver the
    quest's failure; the live bot walks three times (`--retries`), and the hive reads it here."""
    assert cli.RETRIES == 3
