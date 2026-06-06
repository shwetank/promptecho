"""The pytest plugin provides an auto-named cassette per test."""


def test_fixture_auto_names_cassette(pytester):
    pytester.makepyfile(
        test_inner="""
        def test_summarize(tapelog_cassette):
            # fixture resolves and names the cassette after the test
            assert tapelog_cassette.path.replace("\\\\", "/").endswith(
                "cassettes/test_summarize.yaml"
            )
        """
    )
    result = pytester.runpytest()  # plugin auto-loads via its entry point
    result.assert_outcomes(passed=1)
