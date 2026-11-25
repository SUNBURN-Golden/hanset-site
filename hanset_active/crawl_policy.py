"""Network policy shared by the collector and continuous discovery."""
from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

NAVER_DOMAINS = frozenset({'naver.com', 'naver.me', 'naver.net', 'pstatic.net'})

_NEWS_DOMAINS = frozenset(
    entry for line in (Path(__file__).parent / 'profiles' / 'kr_news_domains.txt').read_text(
        encoding='utf-8').splitlines()
    if (entry := line.partition('#')[0].strip().lower().rstrip('.'))
)


def is_naver(url):
    host = (urlsplit(url).hostname or '').lower().rstrip('.')
    return any(host == domain or host.endswith('.' + domain) for domain in NAVER_DOMAINS)


def is_korean_news(url):
    host = (urlsplit(url).hostname or '').lower().rstrip('.')
    labels = host.split('.')
    return any('.'.join(labels[index:]) in _NEWS_DOMAINS for index in range(len(labels)))


korean_news = is_korean_news


@dataclass(frozen=True)
class FetchPolicy:
    use_daily_budgets: bool = True
    preserve_path: bool = False
    retry_direct: bool = False
    allow_short: bool = False

LEGACY = FetchPolicy(retry_direct=True)
COLLECTION = FetchPolicy(use_daily_budgets=False, retry_direct=True)
DISCOVERY = FetchPolicy(use_daily_budgets=False, preserve_path=True, retry_direct=True, allow_short=True)


def _robots_path(value):
    return re.sub(r'%[0-9a-fA-F]{2}', lambda match: match[0].upper(),
                  quote(value, safe="/~!$&'()*+,;=:@%?-._"))


class KoreanNewsRobots:
    """Select the applicable groups and apply the longest matching rule."""
    def __init__(self, body, *, user_agent):
        import math

        product = user_agent.split("/", 1)[0].casefold()
        groups = []
        agents, rules, delay = [], [], 0
        group_has_directives = False
        for line in body.splitlines():
            key, _, value = line.split('#', 1)[0].partition(':')
            key, value = key.strip().lower(), value.strip()
            if key == 'user-agent':
                if group_has_directives:
                    groups.append((agents, rules, delay))
                    agents, rules, delay = [], [], 0
                    group_has_directives = False
                agents.append(value.casefold())
            elif agents and key in ('allow', 'disallow'):
                group_has_directives = True
                if not value:
                    continue
                pattern = _robots_path(value.removesuffix('$'))
                regex = '^' + '.*'.join(map(re.escape, pattern.split('*')))
                if value.endswith('$'):
                    regex += '$'
                rules.append((len(pattern.replace('*', '')), key == 'allow', re.compile(regex)))
            elif agents and key == 'crawl-delay':
                group_has_directives = True
                try:
                    parsed = float(value)
                except ValueError:
                    continue
                if math.isfinite(parsed) and parsed >= 0:
                    delay = max(delay, parsed)
        groups.append((agents, rules, delay))
        applicable = [
            (len(agent) if agent != '*' else 0, rules, delay)
            for agents, rules, delay in groups for agent in agents
            if agent and (agent == '*' or product.startswith(agent))
        ]
        longest = max((size for size, _, _ in applicable), default=0)
        self.rules = [rule for size, rules, _ in applicable if size == longest for rule in rules]
        self.delay = max((delay for size, _, delay in applicable if size == longest), default=0)

    def can_fetch(self, url):
        parts = urlsplit(url)
        path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
        path = _robots_path(path)
        return max(((length, allow) for length, allow, pattern in self.rules if pattern.search(path)),
                   default=(0, True))[1]
