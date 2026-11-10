"""Command line interface for the independent active collection data store."""
import argparse
import json
from pathlib import Path
from .config import Settings
from .db import connect
from .fetcher import Fetcher
from . import venues
from .runner import run
from .report import report, status, export_service_data, sanitize_output
from .connectors.instagram import InstagramConnector, ConnectorPaused
from .util.pii import strip_pii
from .maintenance import prune_offsite_followups


def parser():
    p = argparse.ArgumentParser(prog='python -m hanset_active')
    p.add_argument('--data-dir',type=Path)
    p.add_argument('--profile',type=Path)
    commands = p.add_subparsers(dest='command',required=True)
    commands.add_parser('init')
    seed = commands.add_parser('seed')
    seed.add_argument('--refreeze',action='store_true')
    seed.add_argument('--csv',action='append',default=[],type=Path)
    seed.add_argument('--approved-sources',type=Path,default=Path('/workspace/hanset-collector/approved_sources.json'))
    r = commands.add_parser('run')
    r.add_argument('--max-minutes',type=float)
    r.add_argument('--max-requests',type=int)
    r.add_argument('--classes')
    r.add_argument('--dry-run',action='store_true')
    commands.add_parser('status')
    commands.add_parser('report').add_argument('--json',action='store_true')
    commands.add_parser('gaps').add_argument('--venue')
    commands.add_parser('tasks')
    commands.add_parser('failures')
    commands.add_parser('prune-followups').add_argument('--apply',action='store_true')
    commands.add_parser('export-service-data').add_argument('--out',type=Path,required=True)
    commands.add_parser('ig-stub-check')
    collect = commands.add_parser('collect')
    modes = collect.add_subparsers(dest='collect_command', required=True)
    web = modes.add_parser('web')
    web.add_argument('--targets', type=Path)
    ingest = modes.add_parser('ingest')
    ingest.add_argument('--inbox', type=Path)
    modes.add_parser('status')
    report = modes.add_parser('report')
    report.add_argument('--policy', type=Path)
    promote = modes.add_parser('promote')
    promote.add_argument('--policy', type=Path, required=True)
    promote.add_argument('--dry-run', action='store_true')
    plan = modes.add_parser('plan-gaps')
    plan.add_argument('--ranking', choices=('model', 'local'), default='model')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'collect':
        return _collect_main(args)
    options = {k:v for k,v in {'data_dir':args.data_dir,'profile_path':args.profile}.items() if v is not None}
    try:
        settings = Settings(**options)
        db = connect(settings.data_dir/'active.sqlite3')
        try:
            command = args.command
            if command=='init': result = {'state':'initialized'}
            elif command=='seed':
                paths = args.csv or [p for name in ('saunas.csv','boxes.csv') if (p:=Path(__file__).resolve().parents[1]/name).is_file()]
                result = {'seed_id':venues.seed(db,paths=paths,approved_sources=args.approved_sources,refreeze=args.refreeze)}
            elif command=='run':
                fetcher = Fetcher(settings,db)
                try: result = run(fetcher,max_minutes=args.max_minutes,max_requests=args.max_requests,classes=args.classes,dry_run=args.dry_run)
                finally: fetcher.close()
            elif command=='status': result = status(db)
            elif command=='prune-followups': result = prune_offsite_followups(db,apply=args.apply)
            elif command=='report': result = report(db)
            elif command=='gaps':
                result = [dict(r) for r in db.execute('SELECT g.*,v.name FROM gaps g JOIN venues v USING(venue_id)') if not args.venue or args.venue in (r['name'],r['venue_id'])]
            elif command in ('tasks','failures'): result = [dict(r) for r in db.execute('SELECT * FROM '+command)]
            elif command=='export-service-data': result = {'files':export_service_data(db,args.out)}
            else:
                try: InstagramConnector().read_profile('')
                except ConnectorPaused: result = {'state':'paused'}
                else: result = {'state':'stub_only'}
            print(json.dumps(sanitize_output(result),ensure_ascii=False,indent=2))
        finally: db.close()
    except (ValueError,OSError) as error:
        print(json.dumps({'error':strip_pii(str(error))}))
        return 2
    return 0


def _collect_main(args):
    """Dispatch before the active collector opens its store for unrelated commands."""
    from .collect import CollectError
    from .collect.service import _review_error, load_settings, public_result, run_ingest, run_promote, run_report, run_status, run_web
    review_command = args.collect_command in ('report', 'promote')
    try:
        options = {'active_dir': args.data_dir} if args.data_dir else {}
        if review_command:
            # Offline evidence review does not depend on model configuration.
            from .collect.config import DEFAULT_MODEL
            options['model'] = DEFAULT_MODEL
        settings = load_settings(**options)
        if args.collect_command == 'web':
            result = run_web(settings, targets_path=args.targets)
        elif args.collect_command == 'plan-gaps':
            from .collect.plan import run_plan
            result = run_plan(settings, ranking=args.ranking)
        elif args.collect_command == 'ingest':
            result = run_ingest(settings, inbox_file=args.inbox)
        elif args.collect_command in ('report', 'promote'):
            if args.collect_command == 'promote':
                result = run_promote(settings, policy_path=args.policy, dry_run=args.dry_run)
            else:
                result = run_report(settings, policy_path=args.policy)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get('state') == 'ok' else 2
        else:
            result = run_status(settings)
    except CollectError as error:
        result = (_review_error(error.reason) if review_command else
                  public_result({'state': 'error', 'reason': error.reason, 'success': False, 'collected': False, 'mode': 'web'}))
    except Exception:
        result = (_review_error('runtime_error') if review_command else
                  public_result({'state': 'error', 'reason': 'runtime_error', 'success': False, 'collected': False, 'mode': 'web'}))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('state') in {'done', 'already_complete', 'already_running', 'idle'} else 2
