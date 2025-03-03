import asyncio
import logging
import functools
import random

import disnake
from disnake.ext import commands

from tle.util import codeforces_api as cf
from tle.util import clist_api as clist
from tle.util import db
from tle.util import tasks

logger = logging.getLogger(__name__)

_CF_COLORS = (0xFFCA1F, 0x198BCC, 0xFF2020)
_SUCCESS_GREEN = 0x28A745
_ALERT_AMBER = 0xFFBF00


def embed_neutral(desc, color=disnake.Embed.Empty):
    return disnake.Embed(description=str(desc), color=color)


def embed_success(desc):
    return disnake.Embed(description=str(desc), color=_SUCCESS_GREEN)


def embed_alert(desc):
    return disnake.Embed(description=str(desc), color=_ALERT_AMBER)


def random_cf_color():
    return random.choice(_CF_COLORS)


def cf_color_embed(**kwargs):
    return disnake.Embed(**kwargs, color=random_cf_color())

def color_embed(**kwargs):
    return disnake.Embed(**kwargs, color=random.choice(_CF_COLORS))



def set_same_cf_color(embeds):
    color = random_cf_color()
    for embed in embeds:
        embed.color=color


def attach_image(embed, img_file):
    embed.set_image(url=f'attachment://{img_file.filename}')


def set_author_footer(embed, user):
    embed.set_footer(text=f'Requested by {user}', icon_url=user.display_avatar.url)

def time_format(seconds):
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return days, hours, minutes, seconds


def pretty_time_format(
        seconds,
        *,
        shorten=False,
        only_most_significant=False,
        always_seconds=False):
    days, hours, minutes, seconds = time_format(seconds)
    timespec = [
        (days, 'day', 'days'),
        (hours, 'hour', 'hours'),
        (minutes, 'minute', 'minutes'),
    ]
    timeprint = [(cnt, singular, plural)
                 for cnt, singular, plural in timespec if cnt]
    if not timeprint or always_seconds:
        timeprint.append((seconds, 'second', 'seconds'))
    if only_most_significant:
        timeprint = [timeprint[0]]

    def format_(triple):
        cnt, singular, plural = triple
        return f'{cnt}{singular[0]}' if shorten \
            else f'{cnt} {singular if cnt == 1 else plural}'

    return ' '.join(map(format_, timeprint))


def send_error_if(*error_cls):
    """Decorator for `cog_slash_command_error` methods. Decorated methods send the error in an alert embed
    when the error is an instance of one of the specified errors, otherwise the wrapped function is
    invoked.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(cog, inter, error):
            logging.error(inter.channel.id)
            if isinstance(error, error_cls):
                await inter.send(embed=embed_alert(error))
                error.handled = True
            else:
                await func(cog, inter, error)
        return wrapper
    return decorator

def is_guild_owner_predicate(inter):
    return inter.guild is not None and inter.guild.owner_id == inter.author.id
def is_guild_owner():
    return commands.check(is_guild_owner_predicate)

async def bot_error_handler(inter, exception):
    if getattr(exception, 'handled', False):
        # Errors already handled in cogs should have .handled = True
        return

    if isinstance(exception, db.DatabaseDisabledError):
        await inter.send(embed=embed_alert('Sorry, the database is not available. Some features are disabled.'))
    elif isinstance(exception, commands.NoPrivateMessage):
        await inter.send(embed=embed_alert('Commands are disabled in private channels.'))
    elif isinstance(exception, commands.DisabledCommand):
        await inter.send(embed=embed_alert('Sorry, this command is temporarily disabled.'))
    elif isinstance(exception, commands.NotOwner):
        await inter.send(embed=embed_alert('Sorry, this is an owner-only command :face_with_raised_eyebrow:'))
    elif isinstance(exception, (cf.CodeforcesApiError, commands.UserInputError)):
        await inter.send(embed=embed_alert(exception))
    elif isinstance(exception, (clist.ClistApiError, commands.CheckAnyFailure, commands.CommandOnCooldown)):
        await inter.send(embed=embed_alert(exception))
    else:
        msg = 'Ignoring exception in command {}:'.format(inter.application_command)
        exc_info = type(exception), exception, exception.__traceback__
        logger.exception(msg, exc_info=exc_info)


def once(func):
    """Decorator that wraps the given async function such that it is executed only once."""
    first = True

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            await func(*args, **kwargs)

    return wrapper


def on_ready_event_once(bot):
    """Decorator that uses bot.event to set the given function as the bot's on_ready event handler,
    but does not execute it more than once.
    """
    def register_on_ready(func):
        @bot.event
        @once
        async def on_ready():
            await func()

    return register_on_ready


async def presence(bot):
    await bot.change_presence(activity=disnake.Activity(
        type=disnake.ActivityType.listening,
        name='your commands'))
    await asyncio.sleep(60)

    await bot.change_presence(activity=disnake.Game(
        name='Type /help for usage!'))