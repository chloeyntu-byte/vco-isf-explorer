"""Read-only measurement-source discovery; never creates or changes a source."""
import re
from .spectre_topology import SpectreTopology


def measurement_supplies(topology_file):
    supplies=[]
    if not topology_file:return supplies
    for row in SpectreTopology(topology_file).occurrences():
        if row['model'].lower()!='vsource' or row['hierarchy_path']:continue
        pins=row['terminals']
        # A conventional supply net plus an explicit DC source is sufficient
        # to identify a supply measurement; control/bias sources are excluded.
        if len(pins)!=2 or not re.fullmatch(r'(?:[AD]?VDD|VCC)(?:[!_].*)?',pins[0],re.I):continue
        if row['parameters'].get('type','dc').strip('"').lower()!='dc':continue
        if 'dc' not in row['parameters']:continue
        supplies.append(dict(id=row['name'],positive=pins[0],negative=pins[1],voltage=row['parameters']['dc'],variable=row['parameters']['dc']))
    return supplies
