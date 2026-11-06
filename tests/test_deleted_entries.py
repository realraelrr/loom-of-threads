"""Deletion decisions survive validation without banning shared source records."""
from copy import deepcopy
import unittest

from knowledge_archive.core.model import validate
from test_engine import example


class DeletedEntryValidationTests(unittest.TestCase):
    def data(self):
        data = example()
        data['entries'] = {}
        data['synthesis'] = {}
        data['deleted_entries'] = {'100': {
            'source': 'demo', 'source_id': '100', 'record_id': 'demo:100',
            'reason': 'Superseded', 'deleted_at': '2026-01-01T00:00:00Z',
        }}
        return data

    def test_retained_shared_record_is_valid(self):
        validate(self.data())

    def test_invalid_decisions_rejected(self):
        for field, value in [('reason', ''), ('deleted_at', '2026-01-01'),
                             ('source_id', None), ('record_id', '../bad')]:
            with self.subTest(field=field):
                data = self.data()
                data['deleted_entries']['100'][field] = value
                with self.assertRaises(ValueError):
                    validate(data)

    def test_aliased_entry_cannot_resurrect_deleted_source(self):
        data = self.data()
        data['entries']['new-id'] = example()['entries']['100']
        with self.assertRaisesRegex(ValueError, 'Deleted source'):
            validate(data)

    def test_alias_source_decisions_preserve_each_entry_history(self):
        data = self.data()
        data['deleted_entries']['alias'] = deepcopy(data['deleted_entries']['100'])
        validate(data)
