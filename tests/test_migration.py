"""Archive links must follow deployment and never expose a stale fork link."""
import os
import unittest
from datetime import date
from unittest.mock import patch

from daily_digest.pipeline import PipelineError, archive_link


class ArchiveDeploymentTests(unittest.TestCase):
    def test_card_archive_follows_the_actual_deployment_repository(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "suyoutzy/new-paper-digest"}):
            self.assertEqual(archive_link(date(2026, 9, 28)),
                "https://github.com/suyoutzy/new-paper-digest/blob/main/docs/digests/2026-09-28.md")

    def test_local_preview_does_not_link_to_an_unpublished_archive(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": ""}):
            self.assertEqual(archive_link(date(2026, 9, 28)), "")

    def test_unexpected_repository_values_cannot_construct_misleading_links(self):
        for repository in ("https://evil.example", "owner/repo?redirect=evil", "owner/../repo", "owner/.."):
            with self.subTest(repository=repository), patch.dict(os.environ, {"GITHUB_REPOSITORY": repository}):
                with self.assertRaises(PipelineError):
                    archive_link(date(2026, 9, 28))
