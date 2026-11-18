"""Validation for the Draft-07 subset used by the published ISF contract.

No optional dependency is required. This is deliberately not a general JSON
Schema implementation; unsupported schema vocabulary must not be introduced
without extending its tests. Semantic/electrical checks remain in testbench.py.
"""
import json
import math
from pathlib import Path
import re
from .errors import ValidationError


def validate(value,definition='IsfTestbenchSpec'):
    schema=json.loads((Path(__file__).parents[1]/'contracts/isf-contract.schema.json').read_text())
    def check(v,node,path):
        if '$ref' in node:return check(v,schema['definitions'][node['$ref'].rsplit('/',1)[1]],path)
        kinds=node.get('type',[]);kinds=[kinds] if isinstance(kinds,str) else kinds
        matches={'object':isinstance(v,dict),'array':isinstance(v,list),'string':isinstance(v,str),'integer':isinstance(v,int) and not isinstance(v,bool),'number':isinstance(v,(float,int)) and not isinstance(v,bool) and math.isfinite(v),'boolean':isinstance(v,bool),'null':v is None}
        def fail(reason):raise ValidationError('CONTRACT: '+path+' '+reason)
        if kinds and not any(matches.get(k,False) for k in kinds):fail('invalid type')
        if 'const' in node and v!=node['const']:fail('unsupported value/version')
        if 'enum' in node and v not in node['enum']:fail('invalid choice')
        if isinstance(v,str):
            if len(v)<node.get('minLength',0) or len(v)>node.get('maxLength',float('inf')):fail('invalid length')
            if 'pattern' in node and not re.search(node['pattern'],v):fail('invalid pattern')
        if isinstance(v,(int,float)) and not isinstance(v,bool):
            if not math.isfinite(v):fail('non-finite value')
            if v<node.get('minimum',-math.inf) or v>node.get('maximum',math.inf):fail('out of bounds')
            if 'exclusiveMinimum' in node and v<=node['exclusiveMinimum']:fail('below exclusive minimum')
        if isinstance(v,dict):
            for key in node.get('required',[]):
                if key not in v:fail('missing '+key)
            for key,item in v.items():
                rule=node.get('properties',{}).get(key,node.get('additionalProperties',True))
                if rule is False:fail('unknown field '+key)
                elif isinstance(rule,dict):check(item,rule,path+'/'+key)
        if isinstance(v,list):
            if len(v)<node.get('minItems',0) or len(v)>node.get('maxItems',math.inf):fail('invalid item count')
            if node.get('uniqueItems') and len({json.dumps(x,sort_keys=True) for x in v})!=len(v):fail('duplicate item')
            for index,item in enumerate(v):check(item,node.get('items',{}),path+'/'+str(index))
        for rule in node.get('allOf',[]):
            if 'if' not in rule:check(v,rule,path);continue
            try:check(v,rule['if'],path)
            except ValidationError:
                if 'else' in rule:check(v,rule['else'],path)
            else:
                if 'then' in rule:check(v,rule['then'],path)
    check(value,schema['definitions'][definition],'')
    return value
