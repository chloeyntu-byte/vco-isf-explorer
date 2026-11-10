"""Static HBXF read locations and named families; never execute SKILL.

The user declares the physical meaning of the selected outputs. Naming and
topology observations are descriptive, not an additional signoff gate.
"""

import copy
import re


_VIEW = r'(abs|mag|phase|phaseDeg|phaseDegUnwrapped)'
_DATA = r'getData\s*\(\s*"([^"\\]+)"\s+\?result\s+"([^"\\]+)"\s*\)'
_INDEX = r'([+-]?\d+)'


def parse_transfer_expression(expression):
    """Accept view(harmonic(data, sb)) and harmonic(view(data), sb).

    These forms locate the same raw source and sideband. Their displayed
    calculator values (including unwrapping) are not used as complex H[k].
    """
    text = str(expression or '').strip()
    patterns = (
        (_VIEW + r'\s*\(\s*harmonic\s*\(\s*' + _DATA + r'\s+' + _INDEX + r'\s*\)\s*\)', (0, 1, 2, 3)),
        (r'harmonic\s*\(\s*' + _VIEW + r'\s*\(\s*' + _DATA + r'\s*\)\s+' + _INDEX + r'\s*\)', (0, 1, 2, 3)),
        (r'harmonic\s*\(\s*' + _DATA + r'\s+' + _INDEX + r'\s*\)', (None, 0, 1, 2)),
    )
    for pattern, order in patterns:
        match = re.fullmatch(pattern, text, re.I)
        if match is None:
            continue
        values = match.groups()
        function = values[order[0]].lower() if order[0] is not None else ''
        trace, analysis, sideband = (values[index] for index in order[1:])
        return {'source': trace.lstrip('/').replace('/', '.'), 'expression_source': trace,
                'analysis': analysis, 'sideband': int(sideband),
                'view': 'magnitude' if function in ('abs', 'mag') else 'phase' if function else 'complex',
                'consumption_mode': 'raw_complex_transfer'}
    return None


def output_family_suffix(name):
    """Group common names; expression sidebands remain separate metadata."""
    match = re.fullmatch(r'(.+?)(?:_)?(m\d+|[+-]?\d+)', str(name or ''))
    if match is None:
        return None
    base, suffix = match.groups()
    if not base:
        return None
    return base, -int(suffix[1:]) if suffix.startswith('m') else int(suffix)


def build_transfer_families(outputs, records, record_errors):
    """Group actual names and expose members without physical-consistency gates."""
    groups = {}
    output_names = {item['name'] for item in outputs}
    for output in outputs:
        suffix = output_family_suffix(output['name'])
        if suffix:
            groups.setdefault(suffix[0], []).append((output, suffix[1]))
    families = {}
    for name, entries in groups.items():
        if len(entries) < 2 or not any(item['name'] in records for item, _suffix in entries):
            continue
        errors, notes, members, by_sideband = [], [], [], {}
        for output, suffix in entries:
            output_name = output['name']
            record = records.get(output_name, {})
            member = {key: record.get(key) for key in ('source', 'analysis', 'view', 'sideband')}
            member.update(name=output_name, expression=output.get('expression', ''), suffix_sideband=suffix)
            members.append(member)
            if not record:
                notes.append('%s 的表达式尚不能提取原始读取位置；没有把它作为读取代表项。' % output_name)
                continue
            if output_name in record_errors:
                notes.append('%s：%s' % (output_name, record_errors[output_name]))
            sideband = record['sideband']
            if suffix != sideband:
                notes.append('%s 的名称后缀为 %s，表达式边带为 %s；读取信息采用表达式。' % (output_name, suffix, sideband))
            by_sideband.setdefault(sideband, []).append(output_name)
        for sideband, names in sorted(by_sideband.items()):
            if len(names) > 1:
                notes.append('表达式边带 %s 对应多个名称：%s。' % (sideband, '、'.join(sorted(names))))
        for key, label in (('source', '注入电流源'), ('analysis', '分析'), ('view', '幅值/相位类型')):
            values = {member[key] for member in members if member[key] is not None}
            if len(values) > 1:
                notes.append('系列成员声明的%s有多个：%s；本次完整复数重测采用下方显示的代表项。' % (label, '、'.join(sorted(values))))
        readable = [member for member in members if member['name'] in records and member['name'] not in record_errors]
        readable.sort(key=lambda member: (abs(member['sideband']), member['sideband'], member['name']))
        representative = readable[0]['name'] if readable else None
        if not representative:
            errors.append('系列中无法提取本次结果读取所需的来源、分析及边带。')
        elif 0 not in by_sideband:
            notes.append('没有边带 0 命名输出；使用 %s 定位来源，仍重新测量完整复数响应。' % representative)
        if name in output_names:
            errors.append('系列名 %s 与实际单个 output 重名，无法唯一选择。' % name)
        anchor = records.get(representative, {})
        family = {key: anchor.get(key) for key in ('source', 'analysis', 'view')}
        # Preserve the observed type even if a group has no readable member.
        for key in ('source', 'analysis', 'view'):
            values = {member[key] for member in members if member[key] is not None}
            if family[key] is None and len(values) == 1:
                family[key] = next(iter(values))
        family.update(kind='transfer_family', family_name=name,
                      anchor_output=representative, representative_output=representative,
                      sidebands=sorted(by_sideband),
                      members=sorted(members, key=lambda item: (item['sideband'] is None, item['sideband'] or 0, item['name'])),
                      errors=errors, notes=notes, valid=not errors,
                      eligible=any(records.get(item['name'], {}).get('mode') == 'existing_source' for item in members),
                      consumption_mode='source_binding_remeasured',
                      index_meaning='名称后缀仅用于分组；表达式保留实际 HBXF 边带。当前算法使用 sideband=k-1，不直接按名称后缀作为 ISF 谐波阶数。')
        families[name] = family
    return families


def transfer_choices(outputs, records, record_errors):
    """Return old single-output anchors plus family aliases and UI evidence."""
    families = build_transfer_families(outputs, records, record_errors)
    choices, errors, details = copy.deepcopy(records), dict(record_errors), {}
    for name, record in records.items():
        details[name] = dict(kind='transfer_output', source=record['source'], analysis=record['analysis'],
                             view=record['view'], anchor_output=name, representative_output=name, sidebands=[record['sideband']],
                             members=[dict(name=name, **{key: record[key] for key in ('source', 'analysis', 'view', 'sideband', 'expression')})],
                             errors=[errors[name]] if name in errors else [], notes=[],
                             consumption_mode='source_binding_remeasured')
    for name, family in families.items():
        details[name] = copy.deepcopy(family)
        if family['errors']:
            errors[name] = '系列 %s 无法绑定：%s' % (name, ' '.join(family['errors']))
        else:
            choices[name] = dict(copy.deepcopy(records[family['anchor_output']]), family=copy.deepcopy(family))
            choices[name]['notice'] += ' 已识别系列 %s，读取代表项 %s；表达式边带 %s，成员名不直接作为算法谐波阶数。' % (name, family['anchor_output'], ', '.join(map(str, family['sidebands'])))
    return choices, errors, details, families


def preferred_transfer_choices(choices, errors, families, view, anchor=None):
    """Choose familiar defaults without restricting any explicit user choice."""
    matched = [name for name, record in choices.items() if name not in errors and record.get('view') == view]
    grouped_names = {member['name'] for family in families.values() for member in family['members']}
    family_options = [name for name in matched if name in families]
    candidates = family_options or [name for name in matched if name not in grouped_names]
    if anchor and anchor.get('mode') == 'existing_source':
        same_source = [name for name in candidates if all(choices[name].get(key) == anchor.get(key) for key in ('source', 'analysis'))]
        candidates = same_source or candidates
    if not family_options:
        conventional = [name for name in candidates if choices[name].get('direction_basis') != 'user_declared']
        candidates = conventional or candidates
        if candidates:
            nearest = min(abs(choices[name]['sideband']) for name in candidates)
            candidates = [name for name in candidates if abs(choices[name]['sideband']) == nearest]
    return candidates
