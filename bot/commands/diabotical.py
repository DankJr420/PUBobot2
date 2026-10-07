# -*- coding: utf-8 -*-
"""/link and /unlink commands: map a Discord member to a Diabotical user id.

Only the Discord member id is used to build the mapping; the Diabotical user id
that the member supplies is validated against the Diabotical ``/users`` endpoint
before it is stored. Nicknames are never used to match anything.
"""
from core.database import db
from core.console import log

import bot
from bot.diabotical import report as _report


async def link(ctx, diabotical_id: str):
    """Link the invoking member to a Diabotical user id."""
    diabotical_id = (diabotical_id or "").strip()
    if not diabotical_id:
        raise bot.Exc.SyntaxError(ctx.qc.gt("Please provide a Diabotical user id."))

    client = _report._client.DiaboticalClient()
    if not client.validate_user(diabotical_id):
        raise bot.Exc.NotFoundError(ctx.qc.gt("No Diabotical user found with id `{id}`.").format(
            id=diabotical_id))

    await _report.set_link(ctx.qc.id, ctx.author.id, diabotical_id)
    await ctx.success(ctx.qc.gt("Linked your account to Diabotical user `{id}`.").format(
        id=diabotical_id))


async def unlink(ctx):
    """Unlink the invoking member from Diabotical."""
    removed = await _report.delete_link(ctx.qc.id, ctx.author.id)
    if removed:
        await ctx.success(ctx.qc.gt("Your Diabotical link has been removed."))
    else:
        await ctx.reply(ctx.qc.gt("You have no Diabotical link set."))


async def link_status(ctx):
    """Show the invoking member's Diabotical link (or lack thereof)."""
    data = await db.select_one(("diabotical_id",), "diabotical_links",
                               where={"guild_id": ctx.qc.id, "user_id": ctx.author.id})
    if data and data["diabotical_id"]:
        await ctx.reply(ctx.qc.gt("Your Diabotical user id is `{id}`.").format(
            id=data["diabotical_id"]))
    else:
        await ctx.reply(ctx.qc.gt("You have no Diabotical link set. Use `/link <user_id>`."))
