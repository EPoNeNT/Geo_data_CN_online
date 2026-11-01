"""Verify crawler limits are parsed before any database connection."""
from contextlib import ExitStack, redirect_stderr
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import crawl_certitude


class LimitArgumentTests(unittest.TestCase):
    def run_main(self, arguments=(), environment=None):
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment or {}, clear=True))
            stack.enter_context(patch.object(sys, "argv", ["crawl_certitude.py", *arguments]))
            stack.enter_context(patch.object(crawl_certitude, "setup_logging"))
            stack.enter_context(patch.object(crawl_certitude, "require_env", return_value="unused"))
            connection = stack.enter_context(patch.object(crawl_certitude, "connect_postgres"))
            collect = stack.enter_context(patch.object(crawl_certitude, "collect"))
            error = io.StringIO()
            with redirect_stderr(error):
                try:
                    crawl_certitude.main()
                except SystemExit as exc:
                    connection.assert_not_called()
                    return exc.code, error.getvalue(), collect
            connection.return_value.close.assert_called_once()
            return 0, error.getvalue(), collect

    def test_zero_publication_limit_from_action_or_environment_reaches_collector(self):
        for arguments, environment in ((["--publication-page-limit", "0"], {}),
                                       ([], {"CERTITUDE_PUBLICATION_PAGE_LIMIT": "0"})):
            with self.subTest(arguments=arguments):
                status, _, collect = self.run_main(arguments, environment)
                self.assertEqual(status, 0)
                self.assertEqual(collect.call_args.kwargs["publication_page_limit"], 0)
                self.assertEqual(collect.call_args.kwargs["activity_page_limit"], 100)

    def test_invalid_limits_identify_the_offending_argument(self):
        for flag, value in (("--publication-page-limit", "-1"), ("--activity-page-limit", "0"),
                            ("--publication-page-limit", "invalid")):
            with self.subTest(flag=flag, value=value):
                status, message, collect = self.run_main([flag, value])
                self.assertEqual(status, 2)
                self.assertIn(flag, message)
                collect.assert_not_called()

    def test_cli_value_overrides_invalid_environment_default(self):
        status, _, collect = self.run_main(["--publication-page-limit", "10"],
                                           {"CERTITUDE_PUBLICATION_PAGE_LIMIT": "invalid"})
        self.assertEqual(status, 0)
        self.assertEqual(collect.call_args.kwargs["publication_page_limit"], 10)


if __name__ == "__main__":
    unittest.main()
