from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kbimporter.import_writer import NotionImportWriter


def _make_writer() -> NotionImportWriter:
    with patch("kbimporter.import_writer.Client"):
        writer = NotionImportWriter("fake-key")
    return writer


def _db_item(obj_type: str, id_: str, title: str) -> dict:
    return {
        "object": obj_type,
        "id": id_,
        "title": [{"plain_text": title}],
    }


class TestListDatabases:
    def test_uses_data_source_filter(self):
        """search 调用必须使用新版 data_source filter，不能用已废弃的 database。"""
        writer = _make_writer()
        writer._client.search.return_value = {"results": []}

        writer.list_databases()

        writer._client.search.assert_called_once_with(
            filter={"property": "object", "value": "data_source"}
        )

    def test_returns_data_source_objects(self):
        """object 类型为 data_source 的条目应被收录。"""
        writer = _make_writer()
        writer._client.search.return_value = {
            "results": [_db_item("data_source", "id-1", "我的数据库")]
        }

        result = writer.list_databases()

        assert result == [{"id": "id-1", "title": "我的数据库"}]

    def test_returns_legacy_database_objects(self):
        """兼容旧版：object 类型为 database 的条目也应被收录。"""
        writer = _make_writer()
        writer._client.search.return_value = {
            "results": [_db_item("database", "id-2", "旧数据库")]
        }

        result = writer.list_databases()

        assert result == [{"id": "id-2", "title": "旧数据库"}]

    def test_filters_out_page_objects(self):
        """object 类型为 page 的条目应被忽略。"""
        writer = _make_writer()
        writer._client.search.return_value = {
            "results": [
                _db_item("page", "id-page", "某页面"),
                _db_item("data_source", "id-db", "数据库"),
            ]
        }

        result = writer.list_databases()

        assert len(result) == 1
        assert result[0]["id"] == "id-db"

    def test_empty_results(self):
        writer = _make_writer()
        writer._client.search.return_value = {"results": []}

        assert writer.list_databases() == []

    def test_missing_results_key(self):
        writer = _make_writer()
        writer._client.search.return_value = {}

        assert writer.list_databases() == []

    def test_title_concatenates_multiple_rich_text_spans(self):
        writer = _make_writer()
        writer._client.search.return_value = {
            "results": [
                {
                    "object": "data_source",
                    "id": "id-multi",
                    "title": [
                        {"plain_text": "Hello "},
                        {"plain_text": "World"},
                    ],
                }
            ]
        }

        result = writer.list_databases()

        assert result[0]["title"] == "Hello World"
