import functools
import json
import logging
import math
import time
import datetime
from collections import defaultdict
import itertools
import pytz
from disnake.ext import commands
import disnake

from tle import constants
from tle.util import cache_system2
from tle.util import codeforces_api as cf
from tle.util import clist_api as clist
from tle.util import db
from tle.util import events

logger = logging.getLogger(__name__)

# Connection to database
user_db = None

# Cache system
cache2 = None

# Event system
event_sys = events.EventSystem()

_contest_id_to_writers_map = None

_initialize_done = False

active_groups = defaultdict(set)


async def initialize(nodb):
    global cache2
    global user_db
    global event_sys
    global _contest_id_to_writers_map
    global _initialize_done

    if _initialize_done:
        # This happens if the bot loses connection to Discord and on_ready is triggered again
        # when it reconnects.
        return

    await cf.initialize()

    if nodb:
        user_db = db.DummyUserDbConn()
    else:
        user_db = db.UserDbConn(constants.USER_DB_FILE_PATH)
    
    cache_db = db.CacheDbConn(constants.CACHE_DB_FILE_PATH)

    cache2 = cache_system2.CacheSystem(cache_db)
    await cache2.run()

    try:
        with open(constants.CONTEST_WRITERS_JSON_FILE_PATH) as f:
            data = json.load(f)
        _contest_id_to_writers_map = {contest['id']: [s.lower() for s in contest['writers']] for contest in data}
        logger.info('Contest writers loaded from JSON file')
    except FileNotFoundError:
        logger.warning('JSON file containing contest writers not found')

    _initialize_done = True


# algmyr's guard idea:
def user_guard(*, group, get_exception=None):
    active = active_groups[group]

    def guard(fun):
        @functools.wraps(fun)
        async def f(self, inter, *args, **kwargs):
            user = inter.author.id
            if user in active:
                logger.info(f'{user} repeatedly calls {group} group')
                if get_exception is not None:
                    raise get_exception()
                return
            active.add(user)
            try:
                await fun(self, inter, *args, **kwargs)
            finally:
                active.remove(user)

        return f

    return guard


def is_contest_writer(contest_id, handle):
    if _contest_id_to_writers_map is None:
        return False
    writers = _contest_id_to_writers_map.get(contest_id)
    return writers and handle.lower() in writers


_NONSTANDARD_CONTEST_INDICATORS = [
    'wild', 'fools', 'surprise', 'unknown', 'friday', 'q#', 'testing',
    'marathon', 'kotlin', 'onsite', 'experimental', 'abbyy']

SPECIAL_COUNTRY_NAME_WORD = {
    "u.s.": "U.S.",
    "guinea-bissau": "Guinea-Bissau"
}

# to reformat country names
ARTICLES = ["and", "or", "the"]

def is_nonstandard_contest(contest):
    return any(string in contest.name.lower() for string in _NONSTANDARD_CONTEST_INDICATORS)

def is_nonstandard_problem(problem):
    return (is_nonstandard_contest(cache2.contest_cache.get_contest(problem.contestId)) or
            problem.tag_matches(['*special']))


async def get_visited_contests(handles : [str]):
    """ Returns a set of contest ids of contests that any of the given handles
        has at least one non-CE submission.
    """
    user_submissions = [await cf.user.status(handle=handle) for handle in handles]
    problem_to_contests = cache2.problemset_cache.problem_to_contests

    contest_ids = []
    for sub in itertools.chain.from_iterable(user_submissions):
        if sub.verdict == 'COMPILATION_ERROR':
            continue
        try:
            contest = cache2.contest_cache.get_contest(sub.problem.contestId)
            problem_id = (sub.problem.name, contest.startTimeSeconds)
            contest_ids += problem_to_contests[problem_id]
        except cache_system2.ContestNotFound:
            pass
    return set(contest_ids)

# These are special rated-for-all contests which have a combined ranklist for onsite and online
# participants. The onsite participants have their submissions marked as out of competition. Just
# Codeforces things.
_RATED_FOR_ONSITE_CONTEST_IDS = [
    86,   # Yandex.Algorithm 2011 Round 2 https://codeforces.com/contest/86
    173,  # Croc Champ 2012 - Round 1 https://codeforces.com/contest/173
    335,  # MemSQL start[c]up Round 2 - online version https://codeforces.com/contest/335
]


def is_rated_for_onsite_contest(contest):
    return contest.id in _RATED_FOR_ONSITE_CONTEST_IDS


class ResolveHandleError(commands.CommandError):
    pass


class HandleCountOutOfBoundsError(ResolveHandleError):
    def __init__(self, mincnt, maxcnt):
        super().__init__(f'Number of handles must be between {mincnt} and {maxcnt}')


class FindMemberFailedError(ResolveHandleError):
    def __init__(self, member):
        super().__init__(f'Unable to convert `{member}` to a server member')

class FindRoleFailedError(ResolveHandleError):
    def __init__(self, role):
        super().__init__(f'Unable to convert `{role}` to a server role')

class HandleNotRegisteredError(ResolveHandleError):
    def __init__(self, member, resource='Codeforces'):
        super().__init__(f'{resource} handle for {member.mention} not found in database')


class HandleIsVjudgeError(ResolveHandleError):
    HANDLES = ('vjudge1 vjudge2 vjudge3 vjudge4 vjudge5 '
               'luogu_bot1 luogu_bot2 luogu_bot3 luogu_bot4 luogu_bot5').split()

    def __init__(self, handle):
        super().__init__(f"`{handle}`? I'm not doing that!\n\n(╯°□°）╯︵ ┻━┻")


class FilterError(commands.CommandError):
    pass

class ParamParseError(FilterError):
    pass

def time_format(seconds):
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return days, hours, minutes, seconds


def pretty_time_format(seconds, *, shorten=False, only_most_significant=False, always_seconds=False):
    days, hours, minutes, seconds = time_format(seconds)
    timespec = [
        (days, 'day', 'days'),
        (hours, 'hour', 'hours'),
        (minutes, 'minute', 'minutes'),
    ]
    timeprint = [(cnt, singular, plural) for cnt, singular, plural in timespec if cnt]
    if not timeprint or always_seconds:
        timeprint.append((seconds, 'second', 'seconds'))
    if only_most_significant:
        timeprint = [timeprint[0]]

    def format_(triple):
        cnt, singular, plural = triple
        return f'{cnt}{singular[0]}' if shorten else f'{cnt} {singular if cnt == 1 else plural}'

    return ' '.join(map(format_, timeprint))


def days_ago(t):
    days = (time.time() - t)/(60*60*24)
    if days < 1:
        return 'today'
    if days < 2:
        return 'yesterday'
    return f'{math.floor(days)} days ago'

def reformat_country_name(country):
    country = country.lstrip().rstrip()
    words = country.split()

    for (index, word) in enumerate(words):
        word = word.lower()
        if word in ARTICLES:
            words[index] = word
            continue

        if word in SPECIAL_COUNTRY_NAME_WORD:
            word = SPECIAL_COUNTRY_NAME_WORD.get(word)
        else:
            word = word.capitalize()

        words[index] = word

    return " ".join(words)

async def resolve_handles(inter, converter, handles, *, mincnt=1, maxcnt=5, default_to_all_server=False, resource='codeforces.com'):
    """Convert an iterable of strings to CF handles. A string beginning with ! indicates Discord username,
     otherwise it is a raw CF handle to be left unchanged."""
    role_converter = commands.RoleConverter()
    handles = set(handles)
    if default_to_all_server and not handles:
        handles.add('+server')
    account_ids = set()
    if '+server' in handles:
        handles.remove('+server')
        if resource=='codeforces.com':
            guild_handles = {handle for discord_id, handle
                                in user_db.get_handles_for_guild(inter.guild.id)}
            handles.update(guild_handles)
        else:
            guild_account_ids = {account_id for user_id, account_id, handle 
            in user_db.get_account_ids_for_resource(inter.guild.id, resource=resource)}
            account_ids.update(guild_account_ids)
    if len(account_ids)==0 and (len(handles) < mincnt or (maxcnt and maxcnt < len(handles))):
        raise HandleCountOutOfBoundsError(mincnt, maxcnt)
    resolved_handles = set()
    for handle in handles:
        if handle.startswith('+!'):
            role_identifier = handle[2:]
            try:
                role = await role_converter.convert(inter, role_identifier)
            except commands.errors.CommandError:
                raise FindRoleFailedError(role_identifier)
            for member in inter.guild.members:
                if role in member.roles:
                    if resource=='codeforces.com':
                        handle = user_db.get_handle(member.id, inter.guild.id)
                        if handle is not None:
                            resolved_handles.add(handle)
                    else:
                        account_id = user_db.get_account_id(member.id, inter.guild.id, resource=resource)
                        if account_id is not None:
                            account_ids.add(account_id)
        elif handle.startswith('+'):
            list_name = handle[1:]
            if resource=='codeforces.com':
                list_handles = set(user_db.get_list_handles(list_name=list_name, resource=resource))
                resolved_handles.update(list_handles)
            else:
                list_account_ids = set(user_db.get_list_account_ids(list_name=list_name, resource=resource))
                account_ids.update(list_account_ids)
        elif handle.startswith('!'):
            # ! denotes Discord user
            member_identifier = handle[1:]
            try:
                member = await converter.convert(inter, member_identifier)
            except commands.errors.CommandError:
                raise FindMemberFailedError(member_identifier)
            if resource=='codeforces.com':
                handle = user_db.get_handle(member.id, inter.guild.id)
                if handle is None:
                    raise HandleNotRegisteredError(member)
                resolved_handles.add(handle)
            else:
                account_id = user_db.get_account_id(member.id, inter.guild.id, resource=resource)
                if account_id is None:
                    raise HandleNotRegisteredError(member, resource=resource)
                else:
                    account_ids.add(account_id)
        else:
            if resource=='codeforces.com':
                resolved_handles.add(handle)
            else:
                account_id = user_db.get_account_id_from_handle(handle=handle, resource=resource)
                if account_id is None:
                    resolved_handles.add(handle)
                else:
                    account_ids.add(account_id)
        if handle in HandleIsVjudgeError.HANDLES:
            raise HandleIsVjudgeError(handle)
    if resource=='codeforces.com':
        return list(resolved_handles)
    else:
        if len(resolved_handles)!=0:
            clist_users = await clist.fetch_user_info(resource=resource, handles=list(resolved_handles))
            if clist_users!=None:
                for user in clist_users:
                    account_ids.add(int(user['id']))
        return list(account_ids)

def members_to_handles(members: [disnake.Member], guild_id):
    handles = []
    for member in members:
        handle = user_db.get_handle(member.id, guild_id)
        if handle is None:
            raise HandleNotRegisteredError(member)
        handles.append(handle)
    return handles

def filter_flags(args, params):
    args = list(args)
    flags = [False] * len(params)
    rest = []
    for arg in args:
        try:
            flags[params.index(arg)] = True
        except ValueError:
            rest.append(arg)
    return flags, rest

def negate_flags(*args):
    return [not x for x in args]

def parse_date(arg):
    try:
        if len(arg) == 8:
            fmt = '%d%m%Y'
        elif len(arg) == 6:
            fmt = '%m%Y'
        elif len(arg) == 4:
            fmt = '%Y'
        else:
            raise ValueError
        return time.mktime(datetime.datetime.strptime(arg, fmt).timetuple())
    except ValueError:
        raise ParamParseError(f'{arg} is an invalid date argument')

class SubFilter:
    def __init__(self, rated=True):
        self.team = False
        self.rated = rated
        self.dlo, self.dhi = 0, 10**10
        self.rlo, self.rhi = 500, 3800
        self.types = []
        self.tags = []
        self.notags = []
        self.contests = []
        self.indices = []

    def parse(self, args):
        args = list(set(args))
        rest = []

        for arg in args:
            if arg == '+team':
                self.team = True
            elif arg == '+contest':
                self.types.append('CONTESTANT')
            elif arg =='+outof':
                self.types.append('OUT_OF_COMPETITION')
            elif arg == '+virtual':
                self.types.append('VIRTUAL')
            elif arg == '+practice':
                self.types.append('PRACTICE')
            elif arg[0:2] == 'c+':
                self.contests.append(arg[2:])
            elif arg[0:2] == 'i+':
                self.indices.append(arg[2:])
            elif arg[0] == '+':
                if len(arg) == 1:
                    raise ParamParseError('Problem tag cannot be empty.')
                self.tags.append(arg[1:])
            elif arg[0] == '~':
                if len(arg) == 1:
                    raise ParamParseError('Problem tag cannot be empty.')
                self.notags.append(arg[1:])
            elif arg[0:2] == 'd<':
                self.dhi = min(self.dhi, parse_date(arg[2:]))
            elif arg[0:3] == 'd>=':
                self.dlo = max(self.dlo, parse_date(arg[3:]))
            elif arg[0:3] in ['r<=', 'r>=']:
                if len(arg) < 4:
                    raise ParamParseError(f'{arg} is an invalid rating argument')
                elif arg[1] == '>':
                    self.rlo = max(self.rlo, int(arg[3:]))
                else:
                    self.rhi = min(self.rhi, int(arg[3:]))
                self.rated = True
            else:
                rest.append(arg)

        self.types = self.types or ['CONTESTANT', 'OUT_OF_COMPETITION', 'VIRTUAL', 'PRACTICE']
        return rest

    @staticmethod
    def filter_solved(submissions):
        """Filters and keeps only solved submissions. If a problem is solved multiple times the first
        accepted submission is kept. The unique id for a problem is (problem name, contest start time).
        """
        submissions.sort(key=lambda sub: sub.creationTimeSeconds)
        problems = set()
        solved_subs = []

        for submission in submissions:
            problem = submission.problem
            contest = cache2.contest_cache.contest_by_id.get(problem.contestId, None)
            if submission.verdict == 'OK':
                # Assume (name, contest start time) is a unique identifier for problems
                problem_key = (problem.name, contest.startTimeSeconds if contest else 0)
                if problem_key not in problems:
                    solved_subs.append(submission)
                    problems.add(problem_key)
        return solved_subs

    def filter_subs(self, submissions):
        submissions = SubFilter.filter_solved(submissions)
        filtered_subs = []
        for submission in submissions:
            problem = submission.problem
            contest = cache2.contest_cache.contest_by_id.get(problem.contestId, None)
            type_ok = submission.author.participantType in self.types
            date_ok = self.dlo <= submission.creationTimeSeconds < self.dhi
            tag_ok = not self.tags or problem.tag_matches(self.tags)
            notag_ok = not self.notags or problem.tag_matches_or(self.notags) == None
            index_ok = not self.indices or any(index.lower() == problem.index.lower() for index in self.indices)
            contest_ok = not self.contests or (contest and contest.matches(self.contests))
            team_ok = self.team or len(submission.author.members) == 1
            if self.rated:
                problem_ok = contest and contest.id < cf.GYM_ID_THRESHOLD and not is_nonstandard_problem(problem)
                rating_ok = problem.rating and self.rlo <= problem.rating <= self.rhi
            else:
                # acmsguru and gym allowed
                problem_ok = (not contest or contest.id >= cf.GYM_ID_THRESHOLD
                              or not is_nonstandard_problem(problem))
                rating_ok = True
            if type_ok and date_ok and rating_ok and tag_ok and notag_ok and team_ok and problem_ok and contest_ok and index_ok:
                filtered_subs.append(submission)
        return filtered_subs

    def filter_rating_changes(self, rating_changes):
        rating_changes = [change for change in rating_changes
                    if self.dlo <= change.ratingUpdateTimeSeconds < self.dhi]
        return rating_changes
