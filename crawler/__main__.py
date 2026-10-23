"""python3 -m crawler --help"""
import argparse
import json
from .config import Rejected, load, state_path, external_path
from .store import Store


def main():
    parser = argparse.ArgumentParser(description='HANSET Linux local collector (never publishes)')
    parser.add_argument('--config', help='external local approval registry')
    parser.add_argument('--state', help='external SQLite path')
    commands = parser.add_subparsers(dest='command', required=True)
    report = commands.add_parser('report')
    report.add_argument('--queue', help='read-only legacy inventory report')
    report.add_argument('--format', choices=('json', 'text'), default='json')
    worker = commands.add_parser('worker')
    worker.add_argument('--once', action='store_true', required=True)
    server = commands.add_parser('serve')
    server.add_argument('--bind', default='127.0.0.1')
    server.add_argument('--port', type=int, default=8766)
    candidate = commands.add_parser('candidate')
    candidate.add_argument('url')
    candidate.add_argument('--channel', choices=('auto_discovery', 'browser_extract'), default='auto_discovery')
    verify = commands.add_parser('verify')
    verify.add_argument('record_id')
    verify.add_argument('evidence_ids', nargs='+')
    show = commands.add_parser('show')
    show.add_argument('record_id')
    resolve = commands.add_parser('resolve-conflict')
    resolve.add_argument('conflict_id')
    resolve.add_argument('evidence_id')
    resolve.add_argument('reason_code')
    pending = commands.add_parser('publish-pending')
    pending.add_argument('record_id')
    args = parser.parse_args()
    try:
        cfg = load(args.config)
        if args.command == 'report' and args.queue:
            from .legacy import report as legacy_report
            result = legacy_report(args.queue)
        elif args.command == 'report':
            from .report import report as db_report
            path = external_path(args.state) if args.state else state_path() / 'collector.sqlite3'
            if not __import__('pathlib').Path(path).exists():
                result = {'auto_publish_to_site': False, 'state': 'not_initialized',
                          'stage_counts': {s: 0 for s in __import__('crawler.schema', fromlist=['STAGES']).STAGES}}
            else:
                store = Store(path, readonly=True)
                try: result = db_report(store, cfg)
                finally: store.close()
        else:
            from .local_worker import Worker
            store = Store(args.state)
            w = Worker(store, cfg)
            try:
                if args.command == 'serve':
                    from .intake import serve
                    serve(w, args.bind, args.port)
                    return
                if args.command == 'candidate':
                    result = {'record_id': w.candidate(args.url, channel=args.channel)}
                elif args.command == 'worker':
                    result = {'processed': w.once(), 'auto_publish_to_site': False}
                elif args.command == 'show':
                    row = store.db.execute('SELECT data FROM records WHERE id=?', (args.record_id,)).fetchone()
                    if not row: raise Rejected('unknown_record')
                    result = json.loads(row[0])
                elif args.command == 'resolve-conflict':
                    w.resolve_conflict(args.conflict_id, args.evidence_id, args.reason_code)
                    result = {'resolved': args.conflict_id}
                elif args.command == 'verify':
                    w.verify(args.record_id, args.evidence_ids)
                    result = {'verified': args.record_id}
                else:
                    w.publish_pending(args.record_id)
                    result = {'publish_pending': args.record_id, 'auto_publish_to_site': False}
            finally:
                store.close()
        if getattr(args, 'format', None) == 'text':
            print('auto_publish_to_site: false')
            for stage, count in result['stage_counts'].items(): print(f'{stage}: {count}')
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except Rejected as e:
        print(json.dumps({'error': str(e), 'auto_publish_to_site': False}))
        raise SystemExit(2)


if __name__ == '__main__':
    main()
