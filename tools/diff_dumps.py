#!/usr/bin/env python3
"""Mechanically diff class field layouts between two Il2CppDumper dump.cs files.

Written for the EN 2.2.4 -> 2.2.7 delta sweep: find every wire-carrying class
(*SyncData, DB*Data, *Datas, ...ClientCmd payloads) that gained, lost or moved a
field, so the server can be updated ahead of the next NullReferenceException.

Usage:
  diff_dumps.py OLD_DUMP NEW_DUMP [--filter REGEX] [--all]
"""
import re, sys, argparse, json
from collections import OrderedDict

CLASS_RE = re.compile(r'^(?:[\w\[\]<>, ]*?)\b(?:class|struct)\s+([\w.<>`]+)\s*(?::[^/]*)?(?://.*)?$')
FIELD_RE = re.compile(r'^\t(?:\[[^\]]*\]\s*)?(?:public|private|protected|internal)\s+'
                      r'(?:static\s+|readonly\s+|const\s+|volatile\s+)*'
                      r'(.+?)\s+([\w<>]+)\s*(?:=\s*[^;]+)?;\s*(?://\s*(0x[0-9A-Fa-f]+))?\s*$')
ATTR_RE = re.compile(r'^\t\[(\w+)\](?:\s*//\s*RVA:\s*(0x[0-9A-Fa-f]+))?')


def parse(path):
    """-> {class_name: OrderedDict(field_name -> {type, offset, attrs})}"""
    classes = OrderedDict()
    cur = None
    pending = []          # attributes seen since the last field
    depth = 0
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip('\n')
            if depth == 0:
                m = CLASS_RE.match(line)
                if m and not line.lstrip().startswith('//'):
                    cur = m.group(1)
                    classes.setdefault(cur, OrderedDict())
                    pending = []
                    continue
            # crude brace tracking so nested bodies (methods) don't leak fields
            if line == '{':
                depth += 1
                continue
            if line == '}':
                depth = max(0, depth - 1)
                if depth == 0:
                    cur = None
                continue
            if cur is None or depth != 1:
                continue
            a = ATTR_RE.match(line)
            if a:
                pending.append((a.group(1), a.group(2)))
                continue
            m = FIELD_RE.match(line)
            if m:
                ftype, fname, off = m.group(1).strip(), m.group(2), m.group(3)
                # skip method-ish leftovers
                if '(' in ftype or '(' in fname:
                    pending = []
                    continue
                classes[cur][fname] = {
                    'type': ftype,
                    'offset': off,
                    'attrs': pending,
                }
                pending = []
            else:
                pending = []
    return classes


def json_key_rva(field):
    for name, rva in field['attrs']:
        if name == 'JsonPropertyAttribute':
            return rva
    return None


def is_wire(name):
    """Classes that plausibly cross the wire."""
    n = name.split('.')[-1]
    return (n.endswith('SyncData') or n.endswith('Data') or n.endswith('Datas')
            or n.endswith('Cmd') or n.endswith('Info') or n.endswith('Request')
            or n.endswith('Reply') or n.endswith('Result'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('old'); ap.add_argument('new')
    ap.add_argument('--filter', default=None, help='regex over class names')
    ap.add_argument('--all', action='store_true', help='do not restrict to wire-ish classes')
    ap.add_argument('--json', default=None, help='write machine-readable report here')
    args = ap.parse_args()

    old = parse(args.old)
    new = parse(args.new)
    print(f'# parsed {len(old)} classes from {args.old}', file=sys.stderr)
    print(f'# parsed {len(new)} classes from {args.new}', file=sys.stderr)

    pat = re.compile(args.filter) if args.filter else None

    def want(name):
        if pat and not pat.search(name):
            return False
        return args.all or is_wire(name)

    report = {'added_classes': [], 'removed_classes': [], 'changed': {}}

    for name in new:
        if not want(name):
            continue
        if name not in old:
            report['added_classes'].append(name)
            continue
        of, nf = old[name], new[name]
        added   = [k for k in nf if k not in of]
        removed = [k for k in of if k not in nf]
        moved   = [k for k in nf if k in of and nf[k]['offset'] != of[k]['offset']]
        retyped = [k for k in nf if k in of and nf[k]['type'] != of[k]['type']]
        if added or removed or retyped:
            report['changed'][name] = {
                'added': [{'name': k, 'type': nf[k]['type'], 'offset': nf[k]['offset'],
                           'json_rva': json_key_rva(nf[k])} for k in added],
                'removed': [{'name': k, 'type': of[k]['type']} for k in removed],
                'retyped': [{'name': k, 'old': of[k]['type'], 'new': nf[k]['type']} for k in retyped],
                'moved': len(moved),
            }
    for name in old:
        if want(name) and name not in new:
            report['removed_classes'].append(name)

    for name, d in sorted(report['changed'].items()):
        print(f'\n== {name}')
        for f in d['added']:
            rva = f' json_rva={f["json_rva"]}' if f['json_rva'] else ''
            print(f'  + {f["type"]} {f["name"]};  // {f["offset"]}{rva}')
        for f in d['removed']:
            print(f'  - {f["type"]} {f["name"]};')
        for f in d['retyped']:
            print(f'  ~ {f["name"]}: {f["old"]} -> {f["new"]}')

    if report['added_classes']:
        print(f'\n== new classes ({len(report["added_classes"])})')
        for n in sorted(report['added_classes']):
            print(f'  + {n}')
    if report['removed_classes']:
        print(f'\n== removed classes ({len(report["removed_classes"])})')
        for n in sorted(report['removed_classes']):
            print(f'  - {n}')

    if args.json:
        with open(args.json, 'w') as f:
            json.dump(report, f, indent=1)


if __name__ == '__main__':
    main()
