"""Parameterized queries over a pinned, immutable SQLite snapshot. No model SQL."""
from contextlib import closing
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from .models import *

ROOT = Path(__file__).resolve().parents[1]
NATIONAL = 'dft_stats19_2025_final'
LOCAL = 'ccc_2017_2026h1_20260907'
SOURCES = {
    NATIONAL: SourceNotes(source_id=NATIONAL, title='DfT STATS19 · final 2025', url='https://www.gov.uk/government/statistical-data-sets/road-safety-open-data', period='1 January–31 December 2025', status='national final', caution='Police-reported injury collisions, not all hazards or all injuries. Collision and casualty severity use different units.'),
    LOCAL: SourceNotes(source_id=LOCAL, title='Cambridgeshire Insight · snapshot 7 September 2026', url='https://data.cambridgeshireinsight.org.uk/dataset/road-traffic-collisions', period='1 January 2017–30 June 2026', status='local provisional snapshot', caution='Recent records are provisional. The 2025 local count (216) overlaps the national final count (215); do not add them.'),
}

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()

def normalize(text):
    text = text.lower().replace('saint', 'st').replace('’', "'")
    return re.sub(r'[^a-z0-9 ]', '', text.replace("'", ''))

class Repository:
    def __init__(self, root=ROOT / 'data'):
        self.root = Path(root)
        self.path = self.root / 'roadlens_evidence.sqlite'
        self.hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in [self.path, self.root / 'demo_places.json']}
        self.places = json.loads((self.root / 'demo_places.json').read_text())

    def connect(self):
        db = sqlite3.connect(f'file:{self.path}?mode=ro&immutable=1', uri=True)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        return db

    def location(self, place_id):
        p = next((p for p in self.places if p['place_id'] == place_id), None)
        if p is None:
            raise ValueError('Unknown gazetteer location; request an exact supported junction')
        return ResolvedLocation(place_id=p['place_id'], display_name=p['name'], longitude=p['longitude'], latitude=p['latitude'], easting=p['easting'], northing=p['northing'], coordinate_basis=p['coordinate_basis'], resolver_provenance=p['coordinate_source'])

    def resolve(self, text):
        text = normalize(text)
        # Both street names must occur; a lone New Street is never authoritative.
        return [self.location(p['place_id']) for p in self.places if all(normalize(road.strip()) in text for road in p['name'].split('/'))]

    def query(self, q: EvidenceQuery):
        national = q.source_id == NATIONAL
        table = 'national_collisions' if national else 'local_collisions'
        ident = 'collision_id' if national else 'local_ref'
        where = ['c.date >= ?', 'c.date <= ?']; params = [str(q.start), str(q.end)]
        join = ''; select = f'c.{ident} AS row_id'; people = q.unit != 'collisions'
        if national and (people or q.road_user != 'all'):
            join = ' JOIN national_casualties p ON p.collision_id=c.collision_id'
            if people: select = "c.collision_id || ':' || p.casualty_ref AS row_id"
        if q.collision_id:
            where.append(f'c.{ident}=?'); params.append(q.collision_id)
        if q.road_user != 'all':
            where.append('p.casualty_type=?'); params.append(1 if q.road_user == 'cyclist' else 0)
        if q.severity != 'all':
            if national:
                where.append(('p' if people else 'c') + '.severity=?'); params.append(2 if q.severity == 'serious' else 3)
            else:
                if people: raise ValueError('Local collision severity does not establish individual casualty severity')
                where.append('c.severity=?'); params.append(q.severity.title())
        if q.age == 'under16': where.append('p.age >= 0 AND p.age < 16')
        elif q.age == 'unknown': where.append('p.age IS NULL')
        if q.place_id:
            p = self.location(q.place_id)
            where.append('(c.easting-?)*(c.easting-?)+(c.northing-?)*(c.northing-?) <= ?')
            params.extend([p.easting, p.easting, p.northing, p.northing, q.radius_metres ** 2])
        if not national and people: select += ', c.casualty_count'
        sql = f"SELECT DISTINCT {select} FROM {table} c{join} WHERE {' AND '.join(where)} ORDER BY row_id LIMIT 5000"
        with closing(self.connect()) as db: rows = db.execute(sql, params).fetchall()
        count = sum(r['casualty_count'] for r in rows) if people and not national else len(rows)
        h = digest(q.model_dump(mode='json'))
        return MetricResult(reference_id='metric:' + h[:16], value=count, unit=q.unit, period_start=q.start, period_end=q.end, source_id=q.source_id, provisional=not national, query_hash=h, row_ids=[r['row_id'] for r in rows], query=q)

    def local_record(self, row):
        return CollisionRecord(reference_id=f'{LOCAL}:{row["local_ref"]}', collision_id=row['local_ref'], date=row['date'], latitude=row['latitude'], longitude=row['longitude'], severity=row['severity'], casualty_count=row['casualty_count'], location_text=row['location_text'], junction_detail=row['junction_detail'] or 'Unknown', crossing=row['crossing'] or 'Unknown', source_id=LOCAL, provisional=bool(row['provisional']))

    def nearby(self, location, start=date(2017, 1, 1), end=date(2026, 6, 30), radius_metres=100):
        p = self.location(location.place_id)
        if p != location: raise ValueError('Use the unmodified resolved location')
        if radius_metres not in [50, 100, 250] or not date(2017,1,1) <= start <= end <= date(2026,6,30): raise ValueError('Unsupported radius or period')
        with closing(self.connect()) as db:
            rows = db.execute('SELECT * FROM local_collisions WHERE date>=? AND date<=? AND (easting-?)*(easting-?)+(northing-?)*(northing-?)<=? ORDER BY date DESC,local_ref LIMIT 300', [str(start),str(end),p.easting,p.easting,p.northing,p.northing,radius_metres**2]).fetchall()
        records = [self.local_record(r) for r in rows]
        h = digest([p.place_id, str(start), str(end), radius_metres])
        return CollisionEvidence(reference_id='nearby:'+h[:16], location=p, radius_metres=radius_metres, start=start, end=end, collision_count=len(records), casualty_total=sum(r.casualty_count for r in records), records=records)

    def public_evidence(self):
        metrics = [self.query(EvidenceQuery(unit='people')), self.query(EvidenceQuery(unit='collisions')), self.query(EvidenceQuery(road_user='cyclist')), self.query(EvidenceQuery(road_user='pedestrian'))]
        with closing(self.connect()) as db:
            rows = db.execute('SELECT c.*, SUM(CASE WHEN p.casualty_type=1 THEN 1 ELSE 0 END) AS cyclist_count, SUM(CASE WHEN p.casualty_type=0 THEN 1 ELSE 0 END) AS pedestrian_count FROM national_collisions c JOIN national_casualties p ON p.collision_id=c.collision_id GROUP BY c.collision_id ORDER BY c.collision_id LIMIT 500').fetchall()
        points = [{'id':r['collision_id'],'latitude':r['latitude'],'longitude':r['longitude'],'date':r['date'],'casualty_count':r['casualty_count'],'cyclist_count':r['cyclist_count'],'pedestrian_count':r['pedestrian_count'],'severity':r['severity'],'source_id':NATIONAL} for r in rows]
        return {'metrics':[m.model_dump(mode='json') for m in metrics], 'map_points':points, 'places':[self.location(p['place_id']).model_dump(mode='json') for p in self.places], 'local_context':[self.nearby(self.location(p['place_id'])).model_dump(mode='json') for p in self.places], 'sources':[s.model_dump() for s in SOURCES.values()], 'data_hashes':self.hashes, 'current_imagery':'Current imagery not supplied'}
