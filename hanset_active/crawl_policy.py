"""Network policy shared by the collector and continuous discovery."""
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_NEWS_DOMAINS = frozenset(
    line.strip() for line in
    (Path(__file__).parent / 'profiles' / 'kr_news_domains.txt').read_text().splitlines()
    if line.strip() and not line.startswith('#')
)


def korean_news(url):
    host = (urlsplit(url).hostname or '').lower().rstrip('.')
    labels = host.split('.')
    return any('.'.join(labels[index:]) in _NEWS_DOMAINS for index in range(len(labels)))


@dataclass(frozen=True)
class FetchPolicy:
    news_only: bool = False
    use_daily_budgets: bool = True
    preserve_path: bool = False
    retry_direct: bool = False
    allow_short: bool = False

    def robots(self, url):
        return not self.news_only or korean_news(url)


LEGACY = FetchPolicy()
COLLECTION = FetchPolicy(news_only=True, use_daily_budgets=False, retry_direct=True)
DISCOVERY = FetchPolicy(news_only=True, use_daily_budgets=False, preserve_path=True,
                        retry_direct=True, allow_short=True)



class KoreanNewsRobots:
    """Select the applicable groups and apply the longest matching rule."""
    def __init__(self, body):
        import math
        import re
        from urllib.parse import quote

        groups = []
        agents, rules, delay = [], [], 0
        for line in body.splitlines():
            key, _, value = line.split('#', 1)[0].partition(':')
            key, value = key.strip().lower(), value.strip()
            if key == 'user-agent':
                if rules or delay:
                    groups.append((agents, rules, delay))
                    agents, rules, delay = [], [], 0
                agents.append(value.casefold())
            elif agents and key in ('allow', 'disallow') and value:
                pattern = quote(value.removesuffix('$'), safe="/~!$&'()*+,;=:@%?-._")
                regex = '^' + '.*'.join(map(re.escape, pattern.split('*')))
                if value.endswith('$'):
                    regex += '$'
                rules.append((len(pattern.replace('*', '')), key == 'allow', re.compile(regex)))
            elif agents and key == 'crawl-delay':
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
            if agent and (agent == '*' or 'hansetactivebot'.startswith(agent))
        ]
        longest = max((size for size, _, _ in applicable), default=0)
        self.rules = [rule for size, rules, _ in applicable if size == longest for rule in rules]
        self.delay = max((delay for size, _, delay in applicable if size == longest), default=0)

    def can_fetch(self, user_agent, url):
        from urllib.parse import quote

        parts = urlsplit(url)
        path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
        path = quote(path, safe="/~!$&'()*+,;=:@%?-._")
        return max(((length, allow) for length, allow, pattern in self.rules if pattern.search(path)),
                   default=(0, True))[1]
