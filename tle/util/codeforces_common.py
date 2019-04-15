import asyncio
import json
import logging

import aiohttp

from discord.ext import commands
from collections import defaultdict
from functools import wraps

from tle import constants
from tle.util import codeforces_api as cf
from tle.util import discord_common
from tle.util import handle_conn
from tle.util.cache_system import CacheSystem

logger = logging.getLogger(__name__)

# Connection to database
conn = None
# Cache system
cache = None

_contest_id_to_writers_map = None

active_groups = defaultdict(set)


# algmyr's guard idea:
def user_guard(*, group):
    active = active_groups[group]

    def guard(fun):
        @wraps(fun)
        async def f(self, ctx, *args, **kwargs):
            user = ctx.message.author.id
            if user in active:
                logging.info(f'{user} repeatedly calls {group} group')
                return
            active.add(user)
            try:
                await fun(self, ctx, *args, **kwargs)
            finally:
                active.remove(user)

        return f

    return guard


async def initialize(dbfile, cache_refresh_interval):
    global cache
    global conn
    global _contest_id_to_writers_map
    if dbfile is None:
        conn = handle_conn.DummyConn()
        cache = CacheSystem()
    else:
        conn = handle_conn.HandleConn(dbfile)
        cache = CacheSystem(conn)
    # Initial fetch from CF API
    await cache.force_update()
    if cache.contest_last_cache and cache.problems_last_cache:
        logger.info('Initial fetch done, cache loaded')
    else:
        # If fetch failed, load from disk
        logger.info('Loading cache from disk')
        cache.try_disk()
    asyncio.create_task(_cache_refresher_task(cache_refresh_interval))

    jsonfile = f'{constants.FILEDIR}/{constants.CONTEST_WRITERS_JSON_FILE}'
    try:
        with open(jsonfile) as f:
            data = json.load(f)
        _contest_id_to_writers_map = {contest['id']: contest['writers'] for contest in data}
        logger.info('Contest writers loaded from JSON file')
    except FileNotFoundError:
        logger.warning('JSON file containing contest writers not found')


async def _cache_refresher_task(refresh_interval):
    while True:
        await asyncio.sleep(refresh_interval)
        logger.info('Attempting cache refresh')
        await cache.force_update()


def is_contest_writer(contest_id, handle):
    if _contest_id_to_writers_map is None:
        return False
    writers = _contest_id_to_writers_map.get(contest_id)
    return writers and handle in writers


class CodeforcesHandleError(Exception):
    pass


class HandleCountOutOfBoundsError(CodeforcesHandleError):
    pass


class ResolveHandleFailedError(CodeforcesHandleError):
    pass


class RunHandleCoroFailedError(CodeforcesHandleError):
    pass


async def resolve_handles_or_reply_with_error(ctx, converter, handles, *, mincnt=1, maxcnt=5):
    """Convert an iterable of strings to CF handles. A string beginning with ! indicates Discord username,
     otherwise it is a raw CF handle to be left unchanged."""
    if len(handles) < mincnt or maxcnt < len(handles):
        await ctx.send(embed=discord_common.embed_alert(f'Number of handles must be between {mincnt} and {maxcnt}'))
        raise HandleCountOutOfBoundsError(handles, mincnt, maxcnt)
    resolved_handles = []
    for handle in handles:
        if handle.startswith('!'):
            # ! denotes Discord user
            try:
                member = await converter.convert(ctx, handle[1:])
            except commands.errors.CommandError:
                await ctx.send(embed=discord_common.embed_alert(f'Unable to convert `{handle}` to a server member'))
                raise ResolveHandleFailedError(handle)
            handle = conn.gethandle(member.id)
            if handle is None:
                await ctx.send(embed=discord_common.embed_alert(
                    f'Codeforces handle for member {member.display_name} not found in database'))
                raise ResolveHandleFailedError(handle)
        resolved_handles.append(handle)
    return resolved_handles


async def run_handle_related_coro_or_reply_with_error(ctx, handles, coro):
    """Run a coroutine that takes a handle, for each handle in handles. Returns a list of results."""
    results = []
    for handle in handles:
        try:
            res = await coro(handle=handle)
            results.append(res)
            continue
        except aiohttp.ClientConnectionError:
            await ctx.send(embed=discord_common.embed_alert('Error connecting to Codeforces API'))
        except cf.NotFoundError:
            await ctx.send(embed=discord_common.embed_alert(f'Handle not found: `{handle}`'))
        except cf.InvalidParamError:
            await ctx.send(embed=discord_common.embed_alert(f'Not a valid Codeforces handle: `{handle}`'))
        except cf.CodeforcesApiError:
            await ctx.send(embed=discord_common.embed_alert('Codeforces API error.'))
        raise RunHandleCoroFailedError(handle)
    return results
