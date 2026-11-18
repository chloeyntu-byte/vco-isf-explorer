"""Validate the published optim schema subset without installing dependencies.

Call validate(value, 'Evaluation'|'Calibration'|'DesignRef'|'Gate'|'Ranking').
This is not a general-purpose JSON Schema implementation.
"""
import json
import math
from pathlib import Path


def _json_equal(a, b):
    # JSON booleans and numbers are distinct, unlike Python True == 1.
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    return a == b


def validate(value, definition='Evaluation'):
    schema=json.loads((Path(__file__).parents[1]/'schemas/vco.isf-optim.v1.schema.json').read_text())
    def check(v,node,path):
        if '$ref' in node:
            return check(v,schema['$defs'][node['$ref'].rsplit('/',1)[1]],path)
        def fail(reason):
            raise ValueError('OPTIM_CONTRACT '+path+': '+reason)
        types=node.get('type',[]);types=[types] if isinstance(types,str) else types
        matches=dict(object=isinstance(v,dict),array=isinstance(v,list),string=isinstance(v,str),
                     integer=type(v) is int,number=type(v) in (int,float) and math.isfinite(v),
                     boolean=type(v) is bool,null=v is None)
        if types and not any(matches.get(t,False) for t in types):fail('invalid type')
        if 'const' in node and not _json_equal(v,node['const']):fail('invalid version/value')
        if 'enum' in node and not any(_json_equal(v,item) for item in node['enum']):fail('invalid enum')
        if type(v) in (float,int):
            if not math.isfinite(v) or v<node.get('minimum',-math.inf) or v>node.get('maximum',math.inf):fail('invalid number')
            if 'exclusiveMinimum' in node and v<=node['exclusiveMinimum']:fail('below exclusive minimum')
        if isinstance(v,dict):
            for name in node.get('required',[]):
                if name not in v:fail('missing '+name)
            for name,item in v.items():
                rule=node.get('properties',{}).get(name,node.get('additionalProperties',True))
                if rule is False:fail('unknown property '+name)
                if isinstance(rule,dict):check(item,rule,path+'/'+name)
        if isinstance(v,list):
            if len(v)<node.get('minItems',0):fail('too few items')
            if node.get('uniqueItems') and len({json.dumps(x,sort_keys=True) for x in v})!=len(v):fail('duplicate item')
            for i,item in enumerate(v):check(item,node.get('items',{}),path+'/'+str(i))
    check(value,schema['$defs'][definition],'')
    return value
