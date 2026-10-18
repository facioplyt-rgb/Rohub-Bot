"""Sprawdzenie kontraktów biblioteki i granic dostępu bez tokenu / sieci."""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from hub.client import HubClient
from hub.setup import bot_permissions
from hub.store import RuleError
from hub.ui import Action, Form, buttons, field


class DiscordContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = HubClient({'guild_id': 123, 'data_dir': self.tmp.name,
            'max_offers_per_seller': 10, 'max_open_orders_per_buyer': 3,
            'proof_retention_days': 30, 'reminder_hours': 48})
        self.guild = discord.Guild(data={'id': '123', 'name': 'Test', 'owner_id': '999',
            'roles': [{'id': str(n), 'name': str(n), 'permissions': '0', 'position': pos,
                       'color': 0, 'hoist': False, 'managed': False, 'mentionable': False}
                      for pos, n in enumerate([123, 500, 501, 502, 503])],
            'features': ['COMMUNITY'], 'emojis': [], 'stickers': []}, state=self.client._connection)
        self.client.db.save_config(123, {'roles': {'member': 500, 'seller': 501, 'trade': 502, 'admin': 503},
                                        'channels': {'proof_queue': 600, 'reviews': 601}})

    def member(self, ident, roles):
        return discord.Member(data={'user': {'id': str(ident), 'username': str(ident), 'discriminator': '0', 'avatar': None},
            'roles': [str(n) for n in roles], 'joined_at': '2026-01-01T00:00:00+00:00', 'deaf': False, 'mute': False, 'flags': 0},
            guild=self.guild, state=self.client._connection)

    def interaction(self, ident=20, roles=(500,)):
        response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), send_modal=AsyncMock(), defer=AsyncMock())
        return SimpleNamespace(guild=self.guild, guild_id=123, user=self.member(ident, roles), response=response,
                               followup=SimpleNamespace(send=AsyncMock()), channel_id=700)

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_all_commands_serialize_against_real_discord_library(self):
        commands = self.client.tree.get_commands()
        self.assertGreaterEqual(len(commands), 20)
        for command in commands:
            payload = command.to_dict(self.client.tree)
            self.assertLessEqual(len(payload['description']), 100)
            self.assertTrue(payload['dm_permission'] is False)

    async def test_all_panel_buttons_persistent_and_custom_ids_reconstruct(self):
        view = self.client.order_buttons(42)
        self.assertTrue(view.is_persistent())
        for row in view.to_components():
            for item in row['components']:
                match = Action.__discord_ui_compiled_template__.fullmatch(item['custom_id'])
                self.assertIsNotNone(match)
                rebuilt = await Action.from_custom_id(None, SimpleNamespace(label=item['label'], style=discord.ButtonStyle(item['style'])), match)
                self.assertEqual(rebuilt.item.custom_id, item['custom_id'])

    async def test_no_seller_form_for_regular_member(self):
        with self.assertRaises(RuleError):
            await self.client.route(self.interaction(), 'new_public', 0)

    async def test_no_proof_decision_for_regular_member(self):
        with self.assertRaises(RuleError):
            await self.client.route(self.interaction(), 'proof_accept', 1)

    async def test_member_cannot_see_other_order(self):
        offer = self.client.db.create_offer(123, 10, 'Test', 'mapy', {'price': '10 PLN'}, False)
        self.client.db.publish_offer(offer, 100, 101)
        order, _ = self.client.db.reserve_order(123, offer, 30, 3)
        command = self.client.tree.get_command('zamowienie')
        with self.assertRaises(RuleError):
            await command.callback(self.interaction(), order['id'])

    async def test_no_review_modal_until_proof_approved(self):
        offer = self.client.db.create_offer(123, 10, 'Test', 'mapy', {'price': '10 PLN'}, False)
        self.client.db.publish_offer(offer, 100, 101)
        order, _ = self.client.db.reserve_order(123, offer, 20, 3)
        with self.assertRaises(RuleError):
            await self.client.route(self.interaction(), 'review', order['id'])

    async def test_no_private_evidence_in_order_embed(self):
        offer = self.client.db.create_offer(123, 10, 'Test', 'mapy', {'price': '10 PLN'}, False)
        self.client.db.publish_offer(offer, 100, 101)
        order, _ = self.client.db.reserve_order(123, offer, 20, 3)
        self.client.db.set_order_channel(order['id'], 222)
        self.client.db.submit_proof(order['id'], 20, 'TAJNE-DANE-PLATNICZE')
        embed = self.client.order_embed(self.client.db.get('orders', order['id'])).to_dict()
        self.assertNotIn('TAJNE-DANE-PLATNICZE', str(embed))

    async def test_role_permissions_do_not_require_administrator(self):
        perms = bot_permissions()
        self.assertFalse(perms.administrator)
        self.assertTrue(perms.manage_channels)
        self.assertTrue(perms.manage_roles)

    async def test_sale_form_uses_supported_five_inputs(self):
        i = self.interaction(10, (500, 501))
        await self.client.offer_form(i, 'mapy', False)
        modal = i.response.send_modal.call_args.args[0]
        payload = modal.to_dict()
        self.assertEqual(len(payload['components']), 5)

    async def test_concurrent_buy_clicks_create_one_private_channel(self):
        state = self.client._connection
        state.user = discord.ClientUser(state=state, data={'id': '42', 'username': 'Bot', 'discriminator': '0', 'avatar': None, 'bot': True})
        state._guilds[123] = self.guild
        for member in (self.member(42, ()), self.member(10, (500, 501)), self.member(20, (500,))):
            self.guild._members[member.id] = member
        category = discord.CategoryChannel(state=state, guild=self.guild, data={
            'id': '800', 'name': 'Orders', 'type': 4, 'position': 0, 'permission_overwrites': []})
        self.guild._channels[800] = category
        cfg = self.client.db.config(123)
        cfg['categories'] = {'orders': 800}
        self.client.db.save_config(123, cfg)
        offer = self.client.db.create_offer(123, 10, 'Mapa', 'mapy', {'price': '10 PLN'}, False)
        self.client.db.publish_offer(offer, 100, 101)
        created = []

        async def create_channel(guild, name, **kwargs):
            acl = kwargs['overwrites']
            self.assertFalse(acl[guild.default_role].view_channel)
            self.assertTrue(acl[guild.get_member(20)].view_channel)
            self.assertTrue(acl[guild.get_member(10)].view_channel)
            self.assertNotIn(guild.get_role(500), acl)
            channel = discord.TextChannel(state=state, guild=guild, data={'id': '900', 'name': name,
                'type': 0, 'position': 0, 'permission_overwrites': [], 'topic': kwargs['topic'], 'parent_id': '800'})
            guild._channels[900] = channel
            created.append(channel)
            return channel

        self.client.audit = AsyncMock()
        with patch.object(discord.Guild, 'create_text_channel', create_channel), patch.object(discord.TextChannel, 'send', AsyncMock()):
            await asyncio.gather(self.client.buy(self.interaction(), offer), self.client.buy(self.interaction(), offer))
        self.assertEqual(len(created), 1)
        orders = self.client.db.rows('SELECT * FROM orders')
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]['channel'], 900)


if __name__ == '__main__':
    unittest.main()
