"""Local deterministic source/units/location contract. No provider request is made."""
import json
from datetime import date, datetime, timezone
from pathlib import Path
import os
import sqlite3
import unittest

from pydantic import ValidationError
from backend.evidence import Repository, NATIONAL, LOCAL
from backend.models import EvidenceQuery, PrepareRequest
from evals.scoring import oracle_query, grade_evidence

ROOT = Path(os.environ.get('ROADLENS_PROJECT_ROOT', Path(__file__).resolve().parents[1]))


class EvidenceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Repository(ROOT / 'data')
        cls.fixtures = json.loads((ROOT / 'evals/evidence_cases.json').read_text())

    def test_all_twelve_independent_sql_oracles(self):
        with self.repo.connect() as db:
            for case in self.fixtures:
                with self.subTest(case=case['id']):
                    value = db.execute(case['sql_oracle']).fetchone()[0]
                    self.assertEqual(value, case['expected_value'])
                    computed = self.repo.query(oracle_query(case['id']))
                    self.assertEqual(computed.value, value)
                    self.assertEqual(computed.unit, case['unit'])
                    self.assertEqual(computed.source_id, case['source_id'])
                    self.assertEqual(computed.provisional, case['provisional'])
                    self.assertEqual(computed.geography, 'Cambridge district (E07000008)')
                    self.assertTrue(computed.query_hash)
                    self.assertTrue(computed.row_ids)

    def test_people_and_collisions_are_distinct_units(self):
        self.assertEqual(self.repo.query(EvidenceQuery(severity='serious')).value, 55)
        self.assertEqual(self.repo.query(EvidenceQuery(unit='collisions', severity='serious')).value, 52)

    def test_unknown_age_is_not_a_child(self):
        self.assertEqual(self.repo.query(EvidenceQuery(age='under16', unit='people with known age')).value, 15)
        self.assertEqual(self.repo.query(EvidenceQuery(age='unknown')).value, 9)
        with self.repo.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM national_casualties WHERE age < 0').fetchone()[0], 0)

    def test_local_national_overlap_preserved(self):
        self.assertEqual(self.repo.query(EvidenceQuery(unit='collisions')).value, 215)
        self.assertEqual(self.repo.query(EvidenceQuery(source_id=LOCAL, unit='collisions')).value, 216)

    def test_read_only_connection_enforced(self):
        with self.repo.connect() as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('CREATE TABLE eval_forbidden_write (id TEXT)')

    def test_parameters_do_not_accept_sql(self):
        value = self.repo.query(EvidenceQuery(collision_id="'; DROP TABLE national_collisions;--"))
        self.assertEqual(value.value, 0)
        self.assertEqual(self.repo.query(EvidenceQuery(unit='collisions')).value, 215)

    def test_junction_alias_and_ambiguity(self):
        self.assertEqual(self.repo.resolve("Vicarage Terrace at Saint Matthew's Street")[0].place_id, 'vicarage-st-matthews')
        self.assertEqual(self.repo.resolve('New Street'), [])
        self.assertEqual(self.repo.resolve('Cambridge'), [])
        self.assertEqual(self.repo.resolve('Sturton Street at New Street')[0].place_id, 'sturton-new')

    def test_projected_radius_monotonic_and_record_inclusion(self):
        location = self.repo.location('vicarage-st-matthews')
        previous = set()
        for radius in (50, 100, 250):
            result = self.repo.nearby(location, radius_metres=radius)
            ids = {r.collision_id for r in result.records}
            self.assertTrue(previous <= ids)
            self.assertIn('1737997', ids)
            self.assertEqual(result.collision_count, len(ids))
            self.assertEqual(result.casualty_total, sum(r.casualty_count for r in result.records))
            with self.repo.connect() as db:
                oracle = {r[0] for r in db.execute('SELECT local_ref FROM local_collisions WHERE (easting-?)*(easting-?)+(northing-?)*(northing-?) <= ?', [location.easting]*2+[location.northing]*2+[radius**2])}
            self.assertEqual(ids, oracle)
            previous = ids

    def test_no_fabricated_junction_coordinates(self):
        location = self.repo.location('vicarage-st-matthews')
        modified = location.model_copy(update={'easting': location.easting + 1})
        with self.assertRaises(ValueError):
            self.repo.nearby(modified)
        with self.assertRaises(ValueError):
            self.repo.location('invented-junction')

    def test_historic_collision_not_two_serious_casualties(self):
        evidence = self.repo.nearby(self.repo.location('vicarage-st-matthews'))
        collision = next(r for r in evidence.records if r.collision_id == '1737997')
        self.assertEqual(collision.casualty_count, 2)
        self.assertEqual(collision.severity, 'Serious')
        self.assertEqual(collision.date, date(2026, 4, 13))
        self.assertTrue(collision.provisional)
        with self.assertRaises(ValueError):
            self.repo.query(EvidenceQuery(source_id=LOCAL, start=date(2026, 1, 1), end=date(2026, 6, 30), collision_id='1737997', severity='serious'))

    def test_query_source_period_unit_validation(self):
        invalid = [dict(start='2024-01-01'), dict(end='2026-01-01'), dict(start='2025-12-31', end='2025-01-01'),
                   dict(radius_metres=101), dict(geography='London'), dict(source_id='combined'),
                   dict(unit='deaths'), dict(unit='collisions', age='under16'), dict(unit='people with known age'),
                   dict(unit='people in that collision'), dict(source_id=LOCAL, road_user='cyclist')]
        for params in invalid:
            with self.subTest(params=params), self.assertRaises(ValidationError):
                EvidenceQuery(**params)

    def test_transcript_bounds_and_turn_identity(self):
        turn = {'turn_id': 't1', 'text': 'The pavement is blocked.', 'timestamp': datetime.now(timezone.utc)}
        PrepareRequest(session_id='s1', turns=[turn])
        for turns in ([turn, turn], [dict(turn, text='x'*4001)], [dict(turn, timestamp=datetime.now())]):
            with self.assertRaises(ValidationError):
                PrepareRequest(session_id='s1', turns=turns)

    def test_typed_collision_casualty_count_is_valid_evidence(self):
        fixture = next(case for case in self.fixtures if case['id'] == 'junction_casualties')
        nearby = self.repo.nearby(self.repo.location('vicarage-st-matthews'))
        record = next(row for row in nearby.records if row.collision_id == '1737997').model_dump(mode='json')
        response = {'result': {'output': {'record_refs': [record['reference_id']]}, 'evidence': {'records': [record], 'sources': [{'source_id': LOCAL}]}}}
        self.assertTrue(all(grade_evidence(fixture, response, self.repo).values()))
        record['casualty_count'] = 55
        self.assertFalse(all(grade_evidence(fixture, response, self.repo).values()))

    def test_count_alone_cannot_pass_evaluator(self):
        fixture = next(c for c in self.fixtures if c['id'] == 'cyclists_2025')
        m = self.repo.query(oracle_query(fixture['id'])).model_dump(mode='json')
        response = {'result': {'output': {'metric_refs': [m['reference_id']]}, 'evidence': {'metrics': [m], 'sources': [{'source_id': NATIONAL}]}}}
        self.assertTrue(all(grade_evidence(fixture, response, self.repo).values()))
        m['unit'] = 'deaths'
        self.assertFalse(all(grade_evidence(fixture, response, self.repo).values()))
        m['unit'] = 'people'; m['period_end'] = '2026-06-30'
        self.assertFalse(all(grade_evidence(fixture, response, self.repo).values()))


if __name__ == '__main__':
    unittest.main()
