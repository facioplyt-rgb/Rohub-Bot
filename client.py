import asyncio
import io
import json
import logging
import re
import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import tasks

from .store import Store, RuleError
from .setup import configure, ROLE_NAMES
from .ui import Action, Form, buttons, field

LOG = logging.getLogger('hub')
CATS = ('modele', 'mapy', 'skrypty', 'zestawy', 'uslugi')
STATES = {'creating': 'tworzenie kanału', 'open': 'otwarte', 'disputed': 'spór',
          'archived': 'archiwum', 'cancelled': 'anulowane', 'failed': 'błąd utworzenia'}


def safe(text, limit=1800):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(text)))[:limit]


def yes_no(value):
    value = value.lower().strip()
    if value in ('tak', 't', 'yes'):
        return True
    if value in ('nie', 'n', 'no'):
        return False
    raise RuleError('Wpisz „tak” albo „nie”.')


def https_url(value):
    if not value:
        return ''
    parsed = urlparse(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise RuleError('Podgląd musi być pojedynczym publicznym adresem https:// bez danych logowania.')
    return value


class HubClient(discord.Client):
    def __init__(self, settings):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True  # Eksport historii zgłoszeń. Włącz w Developer Portal.
        # Bez Members / Presence; członkowie pobierani pojedynczo w razie potrzeby.
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none(),
                         max_messages=100, member_cache_flags=discord.MemberCacheFlags.none())
        self.settings = settings
        self.guild_id = settings['guild_id']
        self.data_dir = Path(settings['data_dir'])
        self.db = Store(self.data_dir / 'hub.sqlite3')
        self.tree = app_commands.CommandTree(self)
        self.write_lock = asyncio.Lock()
        self.setup_lock = asyncio.Lock()
        self.cooldowns = {}
        self.install_commands()
        self.tree.on_error = self.command_error

    async def setup_hook(self):
        self.add_dynamic_items(Action)
        guild = discord.Object(id=self.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.maintenance.start()

    async def on_ready(self):
        LOG.info('HUB GOTOWY. Serwer %s. Wersja 1.0.0.', self.guild_id)
        # Rezerwacja zostaje, żeby restart w połowie tworzenia kanału nie zgubił zamówienia.
        guild = self.get_guild(self.guild_id)
        if not guild:
            LOG.error('Bot nie jest na serwerze o GUILD_ID. Sprawdź zaproszenie i identyfikator.')
            return
        for order in self.db.rows("SELECT * FROM orders WHERE guild=? AND state='creating'", (guild.id,)):
            channel = next((c for c in guild.text_channels if c.topic == f"hub-order:{order['id']}"), None)
            if channel:
                self.db.set_order_channel(order['id'], channel.id)
                await channel.send('Odzyskano zamówienie po restarcie.', embed=self.order_embed(self.db.get('orders', order['id'])),
                                   view=self.order_buttons(order['id']))

    async def close(self):
        self.maintenance.cancel()
        try:
            await super().close()
        finally:
            self.db.close()

    def check(self, interaction):
        if not interaction.guild or interaction.guild_id != self.guild_id or not isinstance(interaction.user, discord.Member):
            raise RuleError('Ta funkcja jest dostępna tylko na skonfigurowanym serwerze.')

    def cfg(self):
        cfg = self.db.config(self.guild_id)
        if not cfg.get('channels'):
            raise RuleError('Właściciel musi najpierw uruchomić /konfiguruj.')
        return cfg

    def is_staff(self, member, scope='trade'):
        if member.id == member.guild.owner_id or member.guild_permissions.administrator:
            return True
        cfg = self.db.config(member.guild.id)
        ids = cfg.get('roles', {})
        return any(r.id in (ids.get('admin'), ids.get(scope)) for r in member.roles)

    def require_staff(self, interaction, scope='trade'):
        self.check(interaction)
        if not self.is_staff(interaction.user, scope):
            raise RuleError('Nie masz uprawnień do tej czynności.')

    def require_member(self, interaction, seller=False):
        self.check(interaction)
        if self.is_staff(interaction.user, 'admin'):
            return
        cfg = self.cfg()
        required = cfg['roles']['seller' if seller else 'member']
        if not any(r.id == required for r in interaction.user.roles):
            raise RuleError('Potrzebujesz roli sprzedawcy.' if seller else 'Najpierw zaakceptuj regulamin przyciskiem w kanale regulaminu.')

    def own_record(self, table, ident):
        item = self.db.get(table, ident)
        if item.get('guild') != self.guild_id:
            raise RuleError('Ten rekord nie należy do tego serwera.')
        return item

    def throttle(self, user, action, seconds=10):
        key = (user, action)
        now = time.monotonic()
        if now - self.cooldowns.get(key, -seconds) < seconds:
            raise RuleError('Odczekaj kilka sekund przed ponowną próbą.')
        self.cooldowns[key] = now

    async def reply(self, interaction, content=None, **kwargs):
        kwargs.setdefault('ephemeral', True)
        if interaction.response.is_done():
            return await interaction.followup.send(content, **kwargs)
        return await interaction.response.send_message(content, **kwargs)

    async def command_error(self, interaction, error):
        await self.handle_error(interaction, getattr(error, 'original', error))

    async def handle_error(self, interaction, error):
        if isinstance(error, RuleError):
            message = str(error)
        elif isinstance(error, discord.Forbidden):
            message = 'Bot nie ma uprawnień. Sprawdź jego rolę, kolejność ról i dostęp do kanału.'
            LOG.warning('Brak uprawnień dla czynności Discord: %s', type(error).__name__)
        elif isinstance(error, discord.NotFound):
            message = 'Kanał, wiadomość lub rola zostały usunięte. Użyj /diagnostyka albo zgłoś to administracji.'
        else:
            message = 'Wystąpił błąd. Nie ponawiaj płatności. Administracja powinna sprawdzić konsolę bota.'
            LOG.exception('Błąd huba (%s)', type(error).__name__, exc_info=error)
        try:
            await self.reply(interaction, message)
        except discord.HTTPException:
            LOG.warning('Nie udało się przekazać komunikatu użytkownikowi.')

    async def channel(self, key):
        ident = self.cfg()['channels'].get(key)
        if not ident:
            raise RuleError('Brak kanału w konfiguracji. Właściciel powinien uruchomić /konfiguruj.')
        return self.get_channel(ident) or await self.fetch_channel(ident)

    async def audit(self, actor, action, data):
        self.db.audit(self.guild_id, actor, action, data)
        try:
            ch = await self.channel('logs')
            await ch.send(f'**{safe(action, 100)}** • użytkownik `{actor}`\n{safe(json.dumps(data, ensure_ascii=False), 1700)}')
        except (discord.HTTPException, RuleError):
            LOG.warning('Log zapisano w bazie; wysłanie na Discord nie powiodło się.')

    def offer_embed(self, offer):
        details = offer['details']
        embed = discord.Embed(title=f"#{offer['id']} • {offer['title']}"[:250],
            description=safe(details.get('description', '')), colour=0x38A169 if offer['official'] else 0x5865F2)
        embed.add_field(name='Cena', value=safe(details.get('price', 'Do ustalenia'), 100))
        embed.add_field(name='Sprzedawca', value=f"<@{offer['seller']}>")
        embed.add_field(name='Kategoria / wersja', value=f"{offer['category']} / {offer['version']}")
        status = 'SPRAWDZONA PRZEZ ADMINISTRACJĘ HUBA' if offer['official'] else 'NIESPRAWDZONA PRZEZ ADMINISTRACJĘ'
        embed.add_field(name='Status', value=status, inline=False)
        embed.add_field(name='Autorstwo, licencja, wymagania i wsparcie', value=safe(details.get('terms', 'Nie podano'), 1000), inline=False)
        if details.get('preview'):
            embed.add_field(name='Materiały podglądowe', value=details['preview'], inline=False)
        if offer['official']:
            embed.add_field(name='Zakres sprawdzenia', value=f"Weryfikator <@{offer['checked_by']}> • <t:{offer['checked_at']}:d>\n{safe(offer['check_note'], 700)}", inline=False)
        if offer['status'] not in ('active', 'publishing'):
            embed.add_field(name='Sprzedaż niedostępna', value=f"Status: {offer['status']}", inline=False)
        embed.set_footer(text='Płatność i przekazanie produktu na DM. Hub nie obsługuje płatności. Status nie oznacza współpracy z Robloxem.')
        return embed

    def offer_buttons(self, offer):
        items = []
        if offer['status'] in ('active', 'publishing'):
            items.append(('buy', offer['id'], 'Kup', discord.ButtonStyle.success))
        items.extend([('profile', offer['seller'], 'Profil sprzedawcy'),
                      ('watch', offer['id'], 'Obserwuj / przestań'), ('report', offer['id'], 'Zgłoś ofertę')])
        return buttons(*items)

    async def refresh_offer(self, ident):
        offer = self.own_record('offers', ident)
        if not offer['thread'] or not offer['message']:
            return
        try:
            thread = self.get_channel(offer['thread']) or await self.fetch_channel(offer['thread'])
            if thread.archived:
                await thread.edit(archived=False)
            if isinstance(thread.parent, discord.ForumChannel) and thread.parent.available_tags:
                index = 0 if offer['status'] == 'active' else 1
                await thread.edit(applied_tags=thread.parent.available_tags[index:index+1])
            message = await thread.fetch_message(offer['message'])
            await message.edit(embed=self.offer_embed(offer), view=self.offer_buttons(offer))
        except discord.NotFound:
            self.db.execute('UPDATE offers SET thread=NULL,message=NULL WHERE id=?', (ident,))

    async def publish(self, ident):
        offer = self.own_record('offers', ident)
        if offer['status'] != 'publishing':
            return
        forum = await self.channel(f"forum_{'official' if offer['official'] else 'community'}_{offer['category']}")
        if not isinstance(forum, discord.ForumChannel):
            raise RuleError('Kanał ofert musi być kanałem forum.')
        prefix = f"#{ident}-v{offer['version']} "
        if offer['thread']:
            try:
                thread = self.get_channel(offer['thread']) or await self.fetch_channel(offer['thread'])
                if thread.parent_id == forum.id:
                    await thread.edit(archived=False, locked=False, name=(prefix + offer['title'])[:100],
                                      applied_tags=forum.available_tags[:1])
                    msg = await thread.fetch_message(offer['message'])
                    await msg.edit(embed=self.offer_embed(offer), view=self.offer_buttons(offer))
                    self.db.publish_offer(ident, thread.id, msg.id)
                    return
                old_msg = await thread.fetch_message(offer['message'])
                await old_msg.edit(content='Oferta przeniesiona lub ponownie zweryfikowana. Aktualną znajdziesz przez /szukaj.', embed=None, view=None)
                await thread.edit(locked=True, archived=True)
            except discord.NotFound:
                pass
        # Znacznik w nazwie umożliwia odzyskanie po restarcie między API a zapisem do SQLite.
        thread = next((t for t in forum.threads if t.name.startswith(prefix)), None)
        if thread:
            msg = await thread.fetch_message(thread.id)
            await msg.edit(embed=self.offer_embed(offer), view=self.offer_buttons(offer))
        else:
            result = await forum.create_thread(name=(prefix + offer['title'])[:100],
                embed=self.offer_embed(offer), view=self.offer_buttons(offer),
                applied_tags=forum.available_tags[:1], reason='Publikacja oferty huba')
            thread, msg = result.thread, result.message
        self.db.publish_offer(ident, thread.id, msg.id)

    async def announce_offer(self, offer):
        guild = self.get_guild(self.guild_id)
        for row in self.db.rows('SELECT user FROM watches WHERE guild=? AND offer=?', (self.guild_id, offer['id'])):
            try:
                member = guild.get_member(row['user']) or await guild.fetch_member(row['user'])
                await member.send(f"Aktualizacja obserwowanej oferty #{offer['id']}: {safe(offer['title'], 100)}\n"
                                  f"https://discord.com/channels/{guild.id}/{offer['thread']}\nWyłączenie: przycisk Obserwuj pod ofertą.")
            except discord.HTTPException:
                LOG.info('Powiadomienie obserwowanej oferty niedostarczone.')

    def order_embed(self, order):
        snapshot = order['snapshot']
        embed = discord.Embed(title=f"Zamówienie #{order['id']} • {snapshot['title']}"[:250], colour=0x5865F2)
        embed.description = (f"Kupujący: <@{order['buyer']}>\nSprzedawca: <@{order['seller']}>\n"
            f"Cena przy otwarciu: **{safe(snapshot['details'].get('price', 'Do ustalenia'), 100)}**\n"
            f"Wersja oferty: {snapshot['version']}\nStatus: **{STATES.get(order['state'], order['state'])}**")
        embed.add_field(name='Opis z chwili otwarcia', value=safe(snapshot['details'].get('description', ''), 900) or '—', inline=False)
        embed.add_field(name='Warunki z chwili otwarcia', value=safe(snapshot['details'].get('terms', ''), 900) or '—', inline=False)
        embed.add_field(name='Potwierdzenia', value=f"Dostawa: {'tak' if order['delivered'] else 'nie'}\nOdbiór: {'tak' if order['received'] else 'nie'}\nDowód: {order['proof_status']}", inline=False)
        embed.set_footer(text='Płatność i dostawa na DM. Dowód wysyłasz prywatnie przez /dowod. Nie publikuj danych płatniczych na tym kanale.')
        return embed

    def order_buttons(self, ident):
        return buttons(('delivered', ident, 'Produkt przekazany'), ('received', ident, 'Potwierdzam odbiór'),
            ('proof_help', ident, 'Dodaj dowód'), ('review', ident, 'Wystaw / edytuj opinię'),
            ('dispute', ident, 'Zgłoś problem', discord.ButtonStyle.danger),
            ('cancel', ident, 'Anuluj przed dostawą'), ('archive', ident, 'Archiwizuj — administracja'))

    async def member(self, guild, ident):
        return guild.get_member(ident) or await guild.fetch_member(ident)

    async def private_acl(self, guild, *users):
        cfg = self.cfg()
        acl = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
               guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                    attach_files=True, embed_links=True, manage_channels=True, manage_messages=True)}
        for key in ('admin', 'trade'):
            role = guild.get_role(cfg['roles'][key])
            if role:
                acl[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                       attach_files=True, embed_links=True)
        for member in users:
            acl[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                     attach_files=True, embed_links=True)
        return acl

    async def buy(self, interaction, ident):
        self.require_member(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.write_lock:
            # Sprawdzenie uczestnika następuje przed rezerwacją w bazie.
            offer = self.own_record('offers', ident)
            seller = await self.member(interaction.guild, offer['seller'])
            if not self.is_staff(seller, 'admin') and not any(r.id == self.cfg()['roles']['seller'] for r in seller.roles):
                raise RuleError('Sprzedawca nie ma już uprawnień do sprzedaży. Zgłoś ofertę administracji.')
            order, created = self.db.reserve_order(self.guild_id, ident, interaction.user.id,
                self.settings['max_open_orders_per_buyer'])
            if order['channel']:
                channel = self.get_channel(order['channel'])
                if not channel:
                    try:
                        channel = await self.fetch_channel(order['channel'])
                    except discord.NotFound:
                        raise RuleError('Kanał tego zamówienia usunięto. Administracja musi rozpatrzyć sprawę przez /zamowienie.')
                await self.reply(interaction, f"Masz już to zamówienie: {channel.mention}")
                return
            self.throttle(interaction.user.id, 'buy', 15)
            guild = interaction.guild
            channel = next((c for c in guild.text_channels if c.topic == f"hub-order:{order['id']}"), None)
            if not channel:
                category = guild.get_channel(self.cfg()['categories']['orders'])
                if len(category.channels) >= 45:
                    raise RuleError('Kategoria zamówień jest pełna. Administracja powinna zarchiwizować zakończone sprawy.')
                channel = await guild.create_text_channel(f"zamowienie-{order['id']:04d}", category=category,
                    topic=f"hub-order:{order['id']}", overwrites=await self.private_acl(guild, interaction.user, seller),
                    reason='Otwarcie zamówienia')
            self.db.set_order_channel(order['id'], channel.id)
            await channel.send(embed=self.order_embed(self.db.get('orders', order['id'])), view=self.order_buttons(order['id']))
            await self.audit(interaction.user.id, 'order_created', {'order': order['id'], 'offer': ident})
            await self.reply(interaction, f"Zamówienie: {channel.mention}")

    async def show_profile(self, interaction, seller):
        stats = self.db.profile(self.guild_id, seller)
        embed = discord.Embed(title='Profil sprzedawcy', description=f'<@{seller}>', colour=0x5865F2)
        for name, value in [('Potwierdzone zakupy', stats['transactions']), ('Liczba opinii', stats['reviews']),
            ('Unikalni oceniający', stats['buyers']), ('Średnia', f"{stats['average'] or '—'} / 5"), ('Aktywne oferty', stats['offers'])]:
            embed.add_field(name=name, value=str(value))
        reviews = self.db.rows('SELECT * FROM reviews WHERE guild=? AND seller=? AND hidden=0 ORDER BY updated DESC LIMIT 5', (self.guild_id, seller))
        for review in reviews:
            embed.add_field(name=f"Zamówienie #{review['order_id']} • {review['rating']}/5", value=safe(review['comment'], 350) or 'Bez komentarza', inline=False)
        embed.set_footer(text='Dowody zaakceptowane przez administrację. Ocena nie jest gwarancją kolejnej transakcji.')
        await self.reply(interaction, embed=embed)

    async def send_order_notice(self, order, text):
        if order['channel']:
            try:
                ch = self.get_channel(order['channel']) or await self.fetch_channel(order['channel'])
                await ch.send(text, embed=self.order_embed(self.db.get('orders', order['id'])),
                              view=self.order_buttons(order['id']) if order['state'] not in ('cancelled', 'archived') else None)
            except discord.HTTPException:
                LOG.warning('Stan zamówienia zapisano, wiadomość niedostarczona.')

    async def queue_offer(self, ident):
        offer = self.own_record('offers', ident)
        channel = await self.channel('offer_queue')
        token = ident * 100000 + offer['version']
        await channel.send('Sprawdź pliki w odizolowanym projekcie. Nie zatwierdzaj samego opisu.',
            embed=self.offer_embed(offer), view=buttons(
                ('offer_accept', token, 'Zatwierdź', discord.ButtonStyle.success),
                ('offer_reject', token, 'Odrzuć / poprawki', discord.ButtonStyle.danger)))

    async def offer_form(self, interaction, category, official, existing=None):
        self.require_member(interaction, seller=True)
        details = existing['details'] if existing else {}
        fields = [field('title', 'Nazwa produktu', 90, False, True, existing['title'] if existing else ''),
            field('description', 'Opis i zawartość', 1800, True, True, details.get('description', '')),
            field('price', 'Cena z walutą, np. 25 PLN', 80, False, True, details.get('price', '')),
            field('preview', 'Link HTTPS: zdjęcia / film / demonstracja', 400, False, False, details.get('preview', '')),
            field('terms', 'Autor, prawo sprzedaży, licencja, wymagania', 900, True, True, details.get('terms', ''))]

        async def submit(i, values):
            self.require_member(i, seller=True)
            self.throttle(i.user.id, 'offer', 20)
            values['preview'] = https_url(values['preview'])
            await i.response.defer(ephemeral=True, thinking=True)
            async with self.write_lock:
                if existing:
                    current = self.own_record('offers', existing['id'])
                    if current['version'] != existing['version']:
                        raise RuleError('Oferta została w międzyczasie zmieniona. Otwórz formularz ponownie.')
                    self.db.update_offer(current['id'], i.user.id, {k: v for k, v in values.items() if k != 'title'}, self.is_staff(i.user, 'admin'))
                    self.db.execute('UPDATE offers SET title=? WHERE id=?', (values['title'], current['id']))
                    ident = current['id']
                    await self.refresh_offer(ident)
                else:
                    ident = self.db.create_offer(self.guild_id, i.user.id, values['title'], category,
                        {k: v for k, v in values.items() if k != 'title'}, official,
                        self.settings['max_offers_per_seller'])
                offer = self.db.get('offers', ident)
                if offer['status'] == 'pending':
                    await self.queue_offer(ident)
                    await self.reply(i, f'Oferta #{ident} czeka na sprawdzenie administracji.')
                else:
                    await self.publish(ident)
                    offer = self.db.get('offers', ident)
                    await self.reply(i, f"Opublikowano ofertę #{ident}: <#{offer['thread']}>")
                    if existing:
                        await self.announce_offer(offer)
                await self.audit(i.user.id, 'offer_saved', {'offer': ident, 'official_requested': bool(official)})
        await interaction.response.send_modal(Form('Edytuj ofertę' if existing else 'Nowa oferta', fields, submit))

    async def review_form(self, interaction, ident):
        order = self.own_record('orders', ident)
        if interaction.user.id != order['buyer'] or order['proof_status'] != 'approved':
            raise RuleError('Opinia jest dostępna dla kupującego po zatwierdzeniu dowodu przez administrację.')
        old = self.db.one('SELECT * FROM reviews WHERE order_id=?', (ident,)) or {}

        async def submit(i, values):
            self.check(i)
            try:
                rating = int(values['rating'])
            except ValueError:
                raise RuleError('Ocena musi być liczbą od 1 do 5.')
            await i.response.defer(ephemeral=True, thinking=True)
            async with self.write_lock:
                review = self.db.review(ident, i.user.id, rating, yes_no(values['delivered']), yes_no(values['matches']), values['comment'])
                channel = await self.channel('reviews')
                embed = discord.Embed(title=f"Opinia #{review['id']} • {rating}/5", description=safe(values['comment'], 900) or 'Bez komentarza', colour=0x38A169 if rating >= 4 else 0xD69E2E)
                embed.add_field(name='Sprzedawca', value=f"<@{order['seller']}>")
                embed.add_field(name='Kupujący', value=f"<@{order['buyer']}>")
                embed.add_field(name='Produkt', value=f"#{order['offer']} • {safe(order['snapshot']['title'], 150)}")
                embed.add_field(name='Dostawa / zgodność', value=f"{values['delivered']} / {values['matches']}")
                embed.set_footer(text=f"Zamówienie #{ident} • dowód zaakceptowany przez administrację • aktualizacja opinii zapisuje historię")
                message = None
                if review['message']:
                    try:
                        message = await channel.fetch_message(review['message'])
                        await message.edit(embed=embed)
                    except discord.NotFound:
                        pass
                if message is None:
                    message = await channel.send(embed=embed, view=buttons(('profile', order['seller'], 'Profil sprzedawcy')))
                    self.db.execute('UPDATE reviews SET message=? WHERE id=?', (message.id, review['id']))
                await self.reply(i, 'Opinia została zapisana. Jej historia jest dostępna administracji.')
        await interaction.response.send_modal(Form('Opinia o zakupie', [
            field('rating', 'Ocena od 1 do 5', 1, False, True, str(old.get('rating', ''))),
            field('delivered', 'Czy dostarczono produkt? tak / nie', 3, False, True, 'tak' if old.get('delivered') else ''),
            field('matches', 'Czy zgodny z opisem? tak / nie', 3, False, True, 'tak' if old.get('matches') else ''),
            field('comment', 'Komentarz (publiczny)', 900, True, False, old.get('comment', ''))], submit))

    async def ticket_form(self, interaction, offer_id=0, subject='Pomoc'):
        self.require_member(interaction)
        if offer_id:
            self.own_record('offers', offer_id)

        async def submit(i, values):
            self.require_member(i)
            self.throttle(i.user.id, 'ticket', 60)
            await i.response.defer(ephemeral=True, thinking=True)
            async with self.write_lock:
                count = self.db.one("SELECT COUNT(*) AS n FROM tickets WHERE guild=? AND user=? AND state='open'", (self.guild_id, i.user.id))['n']
                if count >= 2:
                    raise RuleError('Masz już dwa otwarte zgłoszenia. Kontynuuj istniejącą sprawę.')
                cur = self.db.execute('INSERT INTO tickets(guild,user,subject,created) VALUES(?,?,?,?)',
                    (self.guild_id, i.user.id, subject + (f' — oferta #{offer_id}' if offer_id else ''), int(time.time())))
                ident = cur.lastrowid
                category = i.guild.get_channel(self.cfg()['categories']['support'])
                try:
                    channel = await i.guild.create_text_channel(f'zgloszenie-{ident:04d}', category=category,
                        topic=f'hub-ticket:{ident}', overwrites=await self.private_acl(i.guild, i.user), reason='Zgłoszenie użytkownika')
                except discord.HTTPException:
                    self.db.execute("UPDATE tickets SET state='failed' WHERE id=?", (ident,))
                    raise
                self.db.execute('UPDATE tickets SET channel=? WHERE id=?', (channel.id, ident))
                await channel.send(f"**{safe(subject, 100)}** • autor <@{i.user.id}>\n{safe(values['text'], 1700)}\n"
                    'Dowody zawierające dane płatnicze przekaż przez /dowod przy zamówieniu.',
                    view=buttons(('ticket_close', ident, 'Zamknij zgłoszenie — administracja')))
                await self.reply(i, f'Zgłoszenie: {channel.mention}')
        await interaction.response.send_modal(Form('Zgłoszenie do administracji', [field('text', 'Opisz sprawę — bez danych płatniczych', 1700)], submit))

    async def decision_form(self, interaction, action, ident):
        scope = 'reviewer' if action.startswith('offer_') else 'trade'
        self.require_staff(interaction, scope)
        if action.startswith('offer_'):
            offer_id, expected = divmod(ident, 100000)
            offer = self.own_record('offers', offer_id)
            if offer['version'] != expected or offer['status'] != 'pending':
                raise RuleError('To nieaktualna wersja zgłoszenia. Sprawdź najnowszą pozycję w kolejce.')
        else:
            proof = self.db.get('proofs', ident)
            self.own_record('orders', proof['order_id'])

        async def submit(i, values):
            self.require_staff(i, scope)
            await i.response.defer(ephemeral=True, thinking=True)
            async with self.write_lock:
                if action.startswith('offer_'):
                    offer = self.own_record('offers', offer_id)
                    if offer['version'] != expected:
                        raise RuleError('Oferta zmieniła się podczas weryfikacji. Sprawdź ją ponownie.')
                    approved = action == 'offer_accept'
                    self.db.decide_offer(offer_id, i.user.id, approved, values['reason'])
                    if approved:
                        await self.publish(offer_id)
                        await self.announce_offer(self.db.get('offers', offer_id))
                    else:
                        await self.refresh_offer(offer_id)
                    try:
                        seller = await self.member(i.guild, offer['seller'])
                        await seller.send(f"Oferta #{offer_id}: {'zatwierdzona' if approved else 'odrzucona / wymaga poprawek'}.\n{safe(values['reason'], 900)}")
                    except discord.HTTPException:
                        pass
                else:
                    approved = action == 'proof_accept'
                    self.db.decide_proof(ident, i.user.id, approved, values['reason'])
                    proof = self.db.get('proofs', ident)
                    order = self.own_record('orders', proof['order_id'])
                    # Szczegóły i załączniki nie są kopiowane na wspólny kanał zamówienia.
                    await self.send_order_notice(order, 'Dowód zatwierdzony — kupujący może wystawić opinię.' if approved else 'Dowód wymaga uzupełnienia. Kupujący otrzyma uzasadnienie prywatnie lub przez /moje.')
                    if approved:
                        member = await self.member(i.guild, order['buyer'])
                        role = i.guild.get_role(self.cfg()['roles']['customer'])
                        if role:
                            await member.add_roles(role, reason='Potwierdzony dowód zakupu')
                    try:
                        buyer = await self.member(i.guild, order['buyer'])
                        await buyer.send(f"Dowód zamówienia #{order['id']}: {'zaakceptowany' if approved else 'odrzucony'}.\n{safe(values['reason'], 900)}")
                    except discord.HTTPException:
                        pass
                await self.audit(i.user.id, action, {'id': ident, 'result': 'zatwierdzono' if approved else 'odrzucono'})
                await self.reply(i, 'Decyzja została zapisana. Przyciski starego zgłoszenia nie zmienią jej ponownie.')
        label = 'Zakres testów, wersja i autorstwo' if action == 'offer_accept' else 'Uzasadnienie / wymagane poprawki'
        await interaction.response.send_modal(Form('Decyzja administracji', [field('reason', label, 900)], submit))

    async def route(self, interaction, action, ident):
        self.check(interaction)
        if action == 'accept':
            role = interaction.guild.get_role(self.cfg()['roles']['member'])
            if not role:
                raise RuleError('Brak roli użytkownika. Właściciel powinien uruchomić /konfiguruj.')
            await interaction.response.defer(ephemeral=True, thinking=True)
            await interaction.user.add_roles(role, reason='Akceptacja regulaminu')
            await self.reply(interaction, 'Zaakceptowano regulamin. Możesz korzystać z huba.')
        elif action == 'sell':
            self.require_member(interaction, seller=True)
            choices = []
            for n, category in enumerate(CATS):
                choices.append(('new_public', n, f'{category} • nieoficjalna'))
            for n, category in enumerate(CATS):
                choices.append(('new_official', n, f'{category} • do sprawdzenia'))
            await self.reply(interaction, 'Wybierz kategorię i rodzaj oferty. Przesyłając ofertę, oświadczasz, że masz prawo ją sprzedawać.', view=buttons(*choices))
        elif action in ('new_public', 'new_official'):
            if ident >= len(CATS):
                raise RuleError('Nieprawidłowa kategoria.')
            await self.offer_form(interaction, CATS[ident], action == 'new_official')
        elif action == 'buy':
            await self.buy(interaction, ident)
        elif action == 'profile':
            await self.show_profile(interaction, ident or interaction.user.id)
        elif action == 'my':
            await self.show_mine(interaction)
        elif action == 'watch':
            self.require_member(interaction)
            self.own_record('offers', ident)
            existing = self.db.one('SELECT user FROM watches WHERE guild=? AND user=? AND offer=?', (self.guild_id, interaction.user.id, ident))
            if existing:
                self.db.execute('DELETE FROM watches WHERE guild=? AND user=? AND offer=?', (self.guild_id, interaction.user.id, ident))
            else:
                self.db.execute('INSERT INTO watches VALUES(?,?,?)', (self.guild_id, interaction.user.id, ident))
            await self.reply(interaction, 'Obserwowanie wyłączone.' if existing else 'Obserwujesz ofertę. Powiadomienia o opublikowanych aktualizacjach trafią na DM, jeśli masz je włączone.')
        elif action in ('offer_accept', 'offer_reject', 'proof_accept', 'proof_reject'):
            await self.decision_form(interaction, action, ident)
        elif action == 'review':
            await self.review_form(interaction, ident)
        elif action == 'proof_help':
            order = self.own_record('orders', ident)
            if interaction.user.id != order['buyer']:
                raise RuleError('Dowód przesyła kupujący.')
            await self.reply(interaction, f'Użyj **/dowod numer:{ident} plik:** i dodaj obraz PNG/JPG/WEBP do 5 MB. Dodaj opis łączący dowód z tą ofertą. Zasłoń zbędne dane osobowe, saldo i numery rachunków. Załącznik trafi wyłącznie do kanału opiekunów transakcji.')
        elif action in ('delivered', 'received', 'cancel', 'archive', 'dispute'):
            order = self.own_record('orders', ident)
            if interaction.user.id not in (order['buyer'], order['seller']) and not self.is_staff(interaction.user, 'trade'):
                raise RuleError('Nie masz dostępu do zamówienia.')
            if action == 'dispute':
                async def submit(i, values):
                    await i.response.defer(ephemeral=True, thinking=True)
                    self.db.order_action(ident, i.user.id, 'dispute', self.is_staff(i.user, 'trade'))
                    await self.send_order_notice(order, f"**Zgłoszono problem:** {safe(values['reason'], 1000)}\nOpiekun transakcji rozpatrzy sprawę.")
                    await (await self.channel('reports')).send(f"Spór w zamówieniu #{ident}: <#{order['channel']}>. Bez dowodów płatniczych na tym kanale.")
                    await self.reply(i, 'Spór zapisano. Nie oznacza to automatycznej kary ani negatywnej oceny.')
                await interaction.response.send_modal(Form('Problem z zamówieniem', [field('reason', 'Opis problemu — bez danych płatniczych', 1000)], submit))
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            if action == 'archive':
                self.require_staff(interaction)
                if order['state'] == 'disputed':
                    raise RuleError('Najpierw rozstrzygnij spór przez /rozstrzygnij.')
                if order['state'] not in ('cancelled', 'archived'):
                    self.db.order_action(ident, interaction.user.id, 'archive', True)
                await self.archive_order(interaction.guild, order)
            else:
                self.db.order_action(ident, interaction.user.id, action, self.is_staff(interaction.user, 'trade'))
                current = self.db.get('orders', ident)
                await self.send_order_notice(current, {'delivered': 'Sprzedawca oznaczył produkt jako przekazany.', 'received': 'Kupujący potwierdził odbiór. Dowód nadal wymaga niezależnego sprawdzenia.', 'cancel': 'Zamówienie anulowano.'}[action])
                if action == 'cancel':
                    await self.archive_order(interaction.guild, current)
            await self.reply(interaction, 'Zapisano zmianę zamówienia.')
        elif action in ('report', 'help', 'apply_seller'):
            await self.ticket_form(interaction, ident if action == 'report' else 0,
                'Zgłoszenie oferty' if action == 'report' else 'Wniosek o rolę sprzedawcy' if action == 'apply_seller' else 'Pomoc / odwołanie')
        elif action == 'ticket_close':
            self.require_staff(interaction)
            ticket = self.own_record('tickets', ident)
            if ticket['state'] != 'open':
                raise RuleError('Zgłoszenie jest już zamknięte.')
            await interaction.response.defer(ephemeral=True, thinking=True)
            ch = self.get_channel(ticket['channel']) or await self.fetch_channel(ticket['channel'])
            await ch.set_permissions(await self.member(interaction.guild, ticket['user']), view_channel=True, send_messages=False, read_message_history=True)
            await ch.edit(name=f"zamkniete-{ident:04d}")
            self.db.execute("UPDATE tickets SET state='closed' WHERE id=?", (ident,))
            await self.audit(interaction.user.id, 'ticket_closed', {'ticket': ident})
            await self.reply(interaction, 'Zgłoszenie zamknięto. Kanał pozostaje do odczytu; usunięcie po eksporcie wykonuje administracja.')
        else:
            raise RuleError('Nieznany przycisk. Sprawdź aktualną wersję bota.')

    async def archive_order(self, guild, order):
        if not order['channel']:
            return
        ch = self.get_channel(order['channel']) or await self.fetch_channel(order['channel'])
        for ident in (order['buyer'], order['seller']):
            try:
                await ch.set_permissions(await self.member(guild, ident), view_channel=True, send_messages=False,
                                         read_message_history=True, attach_files=False)
            except discord.NotFound:
                # Uczestnik opuścił serwer. Zachowujemy historię dla administracji.
                pass
        category = guild.get_channel(self.cfg()['categories']['archive'])
        if category and len(category.channels) < 45:
            await ch.edit(category=category, sync_permissions=False, name=f"archiwum-{order['id']:04d}")
        await ch.send('Zamówienie zamknięto. Kanał jest tylko do odczytu dla uczestników. Historię można pobrać przez /historia.')

    async def show_mine(self, interaction):
        self.require_member(interaction)
        orders = self.db.rows('SELECT * FROM orders WHERE guild=? AND (buyer=? OR seller=?) ORDER BY id DESC LIMIT 15',
            (self.guild_id, interaction.user.id, interaction.user.id))
        offers = self.db.rows('SELECT * FROM offers WHERE guild=? AND seller=? ORDER BY id DESC LIMIT 10', (self.guild_id, interaction.user.id))
        embed = discord.Embed(title='Moje zamówienia i oferty', colour=0x5865F2)
        embed.description = '\n'.join(f"**#{o['id']}** • {safe(o['snapshot']['title'], 60)} • {STATES[o['state']]} • dowód: {o['proof_status']} • "
            f"{'<'+ '#'+str(o['channel'])+'>' if o['channel'] else 'kanał w tworzeniu'}" for o in orders) or 'Brak zamówień.'
        embed.add_field(name='Twoje oferty', value='\n'.join(f"#{o['id']} • {safe(o['title'], 60)} • {o['status']}" for o in offers) or 'Brak ofert.', inline=False)
        rejected = []
        for o in orders:
            if o['buyer'] == interaction.user.id and o['proof_status'] == 'rejected' and o['proof_id']:
                proof = self.db.get('proofs', o['proof_id'])
                rejected.append(f"#{o['id']}: {safe(proof['reason'], 150)}")
        if rejected:
            embed.add_field(name='Dowody do uzupełnienia — prywatnie', value='\n'.join(rejected)[:1000], inline=False)
        await self.reply(interaction, embed=embed)

    async def panels(self, guild):
        cfg = self.cfg()
        panel_data = {
            'welcome': ('Witaj w hubie', 'Modele, mapy, skrypty i usługi. Zacznij od regulaminu i instrukcji. Oferty oficjalne sprawdza administracja huba; pozostałe są niesprawdzone.', None),
            'rules': ('Regulamin huba — zasady podstawowe',
                '1. Sprzedawaj tylko materiały, do których masz prawo.\n'
                '2. Opisuj cenę, zawartość, licencję, wymagania i wsparcie zgodnie z prawdą.\n'
                '3. Zakaz oszustw, kradzionych plików, złośliwego kodu i fałszywych opinii.\n'
                '4. Płatność i przekazanie produktu ustalacie na DM; hub nie przechowuje pieniędzy.\n'
                '5. Opinia wymaga dowodu zaakceptowanego przez administrację. Negatywna opinia jest dozwolona.\n'
                '6. Nie publikuj danych płatniczych. Dowody przesyłaj przez /dowod.\n'
                '7. Spory i odwołania zgłaszaj przez panel pomocy. Zakaz nękania i spamu.\n'
                '8. Status oficjalny oznacza kontrolę administracji huba, nie partnerstwo z Robloxem.\n'
                '9. Dowody są dostępne opiekunom i administratorom; domyślna retencja 30 dni, dłużej przy otwartym sporze.\n'
                'Właściciel powinien uzupełnić zasady przed publicznym otwarciem serwera.', buttons(('accept', 'Akceptuję regulamin'))),
            'howto': ('Jak korzystać z huba',
                '**Zakup:** znajdź ofertę → Kup → kanał zamówienia → płatność/dostawa na DM → /dowod → decyzja administracji → opinia.\n'
                '**Sprzedaż:** wniosek o rolę → panel sprzedawcy → formularz oferty. Oficjalna oferta czeka na weryfikatora.\n'
                '**Problem:** przycisk Zgłoś problem w zamówieniu. Nie płać ponownie z powodu błędu bota.\n'
                '**Historia:** /moje, /zamowienie, /profil.\nFormularz po restarcie otwórz ponownie; stare przyciski nadal działają.', None),
            'buyer_panel': ('Panel kupującego', 'Twoje zamówienia, profil i pomoc.', buttons(('my', 'Moje zamówienia'), ('profile', 'Mój profil'), ('help', 'Pomoc / odwołanie'))),
            'seller_panel': ('Panel sprzedawcy', 'Wystaw produkt lub złóż wniosek o rolę. Zarządzanie ofertą: /oferta.',
                buttons(('sell', 'Wystaw ofertę'), ('my', 'Moje oferty i zamówienia'), ('apply_seller', 'Zostań sprzedawcą'))),
            'search': ('Wyszukiwarka', 'Użyj /szukaj. Możesz wskazać kategorię, sprzedawcę oraz oferty oficjalne.', None),
            'ranking': ('Reputacja sprzedawców', 'Użyj /ranking i /profil. Pokazujemy ocenę wraz z liczbą opinii i różnych kupujących. Brak odpowiedzi nie tworzy pozytywnej opinii.', None),
            'help': ('Pomoc i odwołania', 'Otwórz prywatne zgłoszenie. Spór dotyczący zakupu otwórz przy zamówieniu.', buttons(('help', 'Otwórz zgłoszenie'))),
        }
        for key, (title, description, view) in panel_data.items():
            ch = await self.channel(key)
            embed = discord.Embed(title=title, description=description, colour=0x5865F2)
            ident = cfg.get('panels', {}).get(key)
            if ident:
                try:
                    msg = await ch.fetch_message(ident)
                    await msg.edit(embed=embed, view=view)
                    continue
                except discord.NotFound:
                    pass
            msg = await ch.send(embed=embed, view=view)
            cfg.setdefault('panels', {})[key] = msg.id
            self.db.save_config(guild.id, cfg)

    async def submit_attachment(self, interaction, ident, attachment, description):
        self.require_member(interaction)
        self.own_record('orders', ident)
        if attachment.size > 5 * 1024 * 1024:
            raise RuleError('Dowód może mieć maksymalnie 5 MB. Przytnij obraz i zasłoń zbędne dane.')
        extension = Path(attachment.filename).suffix.lower()
        if extension not in ('.png', '.jpg', '.jpeg', '.webp'):
            raise RuleError('Dozwolone obrazy: PNG, JPG i WEBP. Nie wysyłaj archiwów ani programów.')
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.write_lock:
            proof_id = self.db.submit_proof(ident, interaction.user.id, description)
            try:
                data = await attachment.read()
                if not (data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8\xff') or (data.startswith(b'RIFF') and data[8:12] == b'WEBP')):
                    raise RuleError('Plik nie ma rozpoznanego formatu obrazu. Zapisz zrzut jako PNG lub JPG.')
                ch = await self.channel('proof_queue')
                order = self.db.get('orders', ident)
                message = await ch.send(
                    f"**Dowód #{proof_id} • zamówienie #{ident}**\nKupujący: <@{order['buyer']}> • sprzedawca: <@{order['seller']}>\n"
                    f"Produkt: {safe(order['snapshot']['title'], 100)}\nOpis: {safe(description, 900)}\n"
                    'Załącznik prywatny. Porównaj strony, produkt i kontekst. Nie zatwierdzaj własnej transakcji.',
                    file=discord.File(io.BytesIO(data), filename=f'dowod-{proof_id}{extension}'),
                    view=buttons(('proof_accept', proof_id, 'Zatwierdź dowód', discord.ButtonStyle.success),
                                 ('proof_reject', proof_id, 'Odrzuć / uzupełnij', discord.ButtonStyle.danger)))
                self.db.execute('UPDATE proofs SET staff_message=? WHERE id=?', (message.id, proof_id))
            except Exception:
                self.db.execute("UPDATE proofs SET status='failed' WHERE id=?", (proof_id,))
                self.db.execute("UPDATE orders SET proof_status='none',proof_id=NULL WHERE id=? AND proof_id=?", (ident, proof_id))
                raise
            await self.reply(interaction, 'Dowód przesłano prywatnie do opiekunów transakcji. Oczekuje na ręczne sprawdzenie.')

    def install_commands(self):
        tree = self.tree

        @tree.command(name='pomoc', description='Instrukcja i lista najważniejszych komend huba')
        @app_commands.guild_only()
        async def help_cmd(i: discord.Interaction):
            self.check(i)
            await self.reply(i, '**Kupujący:** /szukaj, /kup, /moje, /zamowienie, /dowod, /opinia, /profil, /ranking.\n'
                '**Sprzedawca:** /wystaw, /oferta.\n**Pomoc:** przycisk w panelu pomocy; spór otworzysz przy zamówieniu.\n'
                '**Personel:** /rola, /kolejka, /rozstrzygnij, /ukryj_opinie, /odpowiedz, /moderuj, /wyczysc, /statystyki, /diagnostyka.\n'
                '**Właściciel:** /konfiguruj, /panele, /backup.\n**Historia:** /historia kanału zamówienia lub zgłoszenia.')

        @tree.command(name='konfiguruj', description='Właściciel: dodaj kanały, role, fora i panele huba')
        @app_commands.guild_only()
        async def setup_cmd(i: discord.Interaction, potwierdz: bool):
            self.check(i)
            if i.user.id != i.guild.owner_id:
                raise RuleError('Tę komendę wykonuje właściciel serwera.')
            if not potwierdz:
                raise RuleError('Komenda dodaje kanały i role opisane w poradniku. Ustaw potwierdz: True, aby ją wykonać.')
            await i.response.defer(ephemeral=True, thinking=True)
            async with self.setup_lock:
                await configure(self, i.guild)
                await self.panels(i.guild)
            await self.reply(i, 'Struktura huba została utworzona. Ustaw rolę bota nad rolami Hub, przypisz personel przez /rola i sprawdź uprawnienia z konta zwykłego użytkownika.')

        @tree.command(name='panele', description='Właściciel: odśwież wiadomości paneli i podstawowego regulaminu')
        @app_commands.guild_only()
        async def panels_cmd(i: discord.Interaction):
            self.check(i)
            if i.user.id != i.guild.owner_id:
                raise RuleError('Tylko właściciel może odświeżyć panele.')
            await i.response.defer(ephemeral=True, thinking=True)
            async with self.setup_lock:
                await self.panels(i.guild)
            await self.reply(i, 'Panele odświeżono. Własne zasady edytuj w hub/client.py w metodzie panels, żeby kolejne odświeżenie ich nie nadpisało.')

        @tree.command(name='rola', description='Administracja: nadaj lub odbierz wybraną rolę huba')
        @app_commands.guild_only()
        @app_commands.choices(rola=[app_commands.Choice(name=v, value=k) for k, v in ROLE_NAMES.items()])
        async def role_cmd(i: discord.Interaction, osoba: discord.Member, rola: str, nadaj: bool, powod: str):
            self.require_staff(i, 'admin')
            if rola in ('admin', 'trade', 'reviewer', 'mod') and i.user.id != i.guild.owner_id:
                raise RuleError('Role personelu nadaje i odbiera właściciel.')
            if len(powod) > 400:
                raise RuleError('Uzasadnienie może mieć maksymalnie 400 znaków.')
            role = i.guild.get_role(self.cfg()['roles'].get(rola, 0))
            if not role:
                raise RuleError('Nie znaleziono roli. Sprawdź /diagnostyka.')
            await i.response.defer(ephemeral=True, thinking=True)
            if nadaj:
                await osoba.add_roles(role, reason=powod)
            else:
                await osoba.remove_roles(role, reason=powod)
                if rola == 'seller':
                    trusted = i.guild.get_role(self.cfg()['roles']['trusted'])
                    if trusted:
                        await osoba.remove_roles(trusted, reason='Odebrano możliwość sprzedaży')
                    for offer in self.db.rows("SELECT * FROM offers WHERE guild=? AND seller=? AND status='active'", (self.guild_id, osoba.id)):
                        self.db.execute("UPDATE offers SET status='paused' WHERE id=?", (offer['id'],))
                        await self.refresh_offer(offer['id'])
            await self.audit(i.user.id, 'role_changed', {'user': osoba.id, 'role': rola, 'grant': nadaj, 'reason': powod})
            await self.reply(i, 'Zapisano zmianę roli.')

        @tree.command(name='wystaw', description='Sprzedawca: otwórz formularz nowej oferty')
        @app_commands.guild_only()
        @app_commands.choices(kategoria=[app_commands.Choice(name=c, value=c) for c in CATS])
        async def create_cmd(i: discord.Interaction, kategoria: str, oficjalna: bool = False):
            await self.offer_form(i, kategoria, oficjalna)

        @tree.command(name='oferta', description='Zarządzaj ofertą: edycja, wstrzymanie, wznowienie i weryfikacja')
        @app_commands.guild_only()
        @app_commands.choices(akcja=[app_commands.Choice(name=v, value=k) for k, v in {
            'show': 'Pokaż', 'edit': 'Edytuj', 'pause': 'Wstrzymaj', 'resume': 'Wznów',
            'close': 'Zamknij', 'submit': 'Zgłoś do oficjalnych', 'retry': 'Ponów publikację / kolejkę'}.items()])
        async def offer_cmd(i: discord.Interaction, numer: int, akcja: str):
            self.check(i)
            offer = self.own_record('offers', numer)
            if akcja == 'show':
                await self.reply(i, embed=self.offer_embed(offer), view=self.offer_buttons(offer))
                return
            self.require_member(i, seller=True)
            if i.user.id != offer['seller'] and not self.is_staff(i.user, 'admin'):
                raise RuleError('To nie jest Twoja oferta.')
            if akcja == 'edit':
                await self.offer_form(i, offer['category'], offer['requested'], offer)
                return
            await i.response.defer(ephemeral=True, thinking=True)
            async with self.write_lock:
                offer = self.own_record('offers', numer)
                if akcja == 'pause' and offer['status'] == 'active':
                    self.db.execute("UPDATE offers SET status='paused' WHERE id=?", (numer,))
                    await self.refresh_offer(numer)
                elif akcja == 'resume' and offer['status'] == 'paused':
                    self.db.execute("UPDATE offers SET status='publishing' WHERE id=?", (numer,))
                    await self.publish(numer)
                elif akcja == 'close':
                    self.db.execute("UPDATE offers SET status='rejected',official=0 WHERE id=?", (numer,))
                    await self.refresh_offer(numer)
                elif akcja == 'submit' and offer['status'] in ('active', 'paused', 'rejected'):
                    self.db.execute("UPDATE offers SET status='pending',requested=1,official=0,version=version+1 WHERE id=?", (numer,))
                    await self.refresh_offer(numer)
                    await self.queue_offer(numer)
                elif akcja == 'retry' and offer['status'] in ('publishing', 'pending'):
                    if offer['status'] == 'publishing':
                        await self.publish(numer)
                    else:
                        await self.queue_offer(numer)
                else:
                    raise RuleError('Ta czynność nie pasuje do obecnego statusu oferty.')
                await self.audit(i.user.id, 'offer_' + akcja, {'offer': numer})
                await self.reply(i, 'Zapisano zmianę oferty.')

        @tree.command(name='szukaj', description='Wyszukaj aktywne oferty huba')
        @app_commands.guild_only()
        @app_commands.choices(kategoria=[app_commands.Choice(name=c, value=c) for c in CATS])
        async def search_cmd(i: discord.Interaction, tekst: str = '', kategoria: str = '', oficjalne: bool | None = None,
                             sprzedawca: discord.Member | None = None, strona: app_commands.Range[int, 1, 10000] = 1):
            self.check(i)
            sql = "SELECT * FROM offers WHERE guild=? AND status='active'"
            args = [self.guild_id]
            if tekst:
                sql += ' AND (title LIKE ? OR details LIKE ?)'
                args.extend([f'%{tekst[:100]}%'] * 2)
            if kategoria:
                sql += ' AND category=?'
                args.append(kategoria)
            if oficjalne is not None:
                sql += ' AND official=?'
                args.append(int(oficjalne))
            if sprzedawca:
                sql += ' AND seller=?'
                args.append(sprzedawca.id)
            sql += ' ORDER BY id DESC LIMIT 10 OFFSET ?'
            args.append((strona - 1) * 10)
            offers = self.db.rows(sql, args)
            embed = discord.Embed(title=f'Oferty • strona {strona}', colour=0x5865F2)
            embed.description = '\n\n'.join(f"**#{o['id']} • {safe(o['title'], 90)}**\n{safe(o['details']['price'], 80)} • {'oficjalna' if o['official'] else 'nieoficjalna'} • <#{o['thread']}>" for o in offers) or 'Brak pasujących ofert.'
            await self.reply(i, embed=embed)

        @tree.command(name='kup', description='Otwórz prywatne zamówienie na podstawie numeru oferty')
        @app_commands.guild_only()
        async def buy_cmd(i: discord.Interaction, numer: int):
            await self.buy(i, numer)

        @tree.command(name='moje', description='Pokaż własne zamówienia, oferty i powody odrzucenia dowodów')
        @app_commands.guild_only()
        async def mine_cmd(i: discord.Interaction):
            await self.show_mine(i)

        @tree.command(name='zamowienie', description='Pokaż zamówienie i jego przyciski')
        @app_commands.guild_only()
        async def order_cmd(i: discord.Interaction, numer: int):
            self.check(i)
            order = self.own_record('orders', numer)
            if i.user.id not in (order['buyer'], order['seller']) and not self.is_staff(i.user):
                raise RuleError('Nie masz dostępu do zamówienia.')
            await self.reply(i, embed=self.order_embed(order), view=self.order_buttons(numer))

        @tree.command(name='dowod', description='Kupujący: prześlij prywatnie dowód zakupu do opiekunów')
        @app_commands.guild_only()
        async def proof_cmd(i: discord.Interaction, numer: int, plik: discord.Attachment, opis: app_commands.Range[str, 5, 900]):
            await self.submit_attachment(i, numer, plik, opis)

        @tree.command(name='opinia', description='Wystaw lub edytuj opinię po zatwierdzeniu dowodu zakupu')
        @app_commands.guild_only()
        async def review_cmd(i: discord.Interaction, zamowienie: int):
            self.check(i)
            await self.review_form(i, zamowienie)

        @tree.command(name='profil', description='Reputacja sprzedawcy i ostatnie opinie')
        @app_commands.guild_only()
        async def profile_cmd(i: discord.Interaction, osoba: discord.Member | None = None):
            self.check(i)
            await self.show_profile(i, osoba.id if osoba else i.user.id)

        @tree.command(name='ranking', description='Sprzedawcy z minimum trzema opiniami od różnych kupujących')
        @app_commands.guild_only()
        async def ranking_cmd(i: discord.Interaction):
            self.check(i)
            rows = self.db.rows('''SELECT seller,COUNT(*) AS n,COUNT(DISTINCT buyer) AS buyers,
                ROUND(AVG(rating),2) AS average FROM reviews WHERE guild=? AND hidden=0
                GROUP BY seller HAVING COUNT(DISTINCT buyer)>=3 ORDER BY average DESC,buyers DESC,n DESC LIMIT 10''', (self.guild_id,))
            await self.reply(i, '\n'.join(f"{n}. <@{row['seller']}> • **{row['average']}/5** • {row['n']} opinii • {row['buyers']} kupujących" for n, row in enumerate(rows, 1)) or 'Ranking pojawi się po zebraniu minimum trzech różnych oceniających przez sprzedawcę.')

        @tree.command(name='kolejka', description='Personel: pokaż numery spraw oczekujących na obsługę')
        @app_commands.guild_only()
        async def queue_cmd(i: discord.Interaction):
            self.check(i)
            self.require_staff(i, 'reviewer') if not self.is_staff(i.user, 'trade') else self.require_staff(i)
            offers = self.db.rows("SELECT id,title FROM offers WHERE guild=? AND status='pending' ORDER BY id LIMIT 20", (self.guild_id,))
            text = '**Oferty:**\n' + ('\n'.join(f"#{o['id']} • {safe(o['title'], 70)}" for o in offers) or 'Brak.')
            if self.is_staff(i.user):
                orders = self.db.rows("SELECT id,proof_status,state FROM orders WHERE guild=? AND (proof_status='pending' OR state='disputed') ORDER BY id LIMIT 20", (self.guild_id,))
                text += '\n\n**Zamówienia:**\n' + ('\n'.join(f"#{o['id']} • {o['proof_status']} • {o['state']}" for o in orders) or 'Brak.')
            await self.reply(i, text)

        @tree.command(name='rozstrzygnij', description='Opiekun: rozstrzygnij spór, zachowując uzasadnienie')
        @app_commands.guild_only()
        async def resolve_cmd(i: discord.Interaction, zamowienie: int, uzasadnienie: app_commands.Range[str, 5, 900]):
            self.require_staff(i)
            order = self.own_record('orders', zamowienie)
            if order['state'] != 'disputed':
                raise RuleError('To zamówienie nie ma otwartego sporu.')
            await i.response.defer(ephemeral=True, thinking=True)
            self.db.order_action(zamowienie, i.user.id, 'resolve', True)
            await self.send_order_notice(order, f'**Rozstrzygnięcie administracji:** {safe(uzasadnienie, 900)}')
            await self.audit(i.user.id, 'dispute_resolved', {'order': zamowienie, 'reason': uzasadnienie})
            await self.reply(i, 'Zapisano rozstrzygnięcie. Nie powoduje to cofnięcia płatności ani automatycznej oceny.')

        @tree.command(name='ukryj_opinie', description='Administracja: ukryj lub przywróć opinię z uzasadnieniem')
        @app_commands.guild_only()
        async def hide_cmd(i: discord.Interaction, numer: int, ukryj: bool, powod: app_commands.Range[str, 5, 500]):
            self.require_staff(i, 'admin')
            review = self.own_record('reviews', numer)
            if i.user.id in (review['buyer'], review['seller']):
                raise RuleError('Własną opinię lub opinię o Tobie musi rozpatrzyć inny administrator.')
            await i.response.defer(ephemeral=True, thinking=True)
            self.db.execute('UPDATE reviews SET hidden=? WHERE id=?', (int(ukryj), numer))
            if review['message']:
                try:
                    msg = await (await self.channel('reviews')).fetch_message(review['message'])
                    if ukryj:
                        await msg.edit(content=f'Opinia #{numer} ukryta przez administrację. Powód: {safe(powod, 500)}', embed=None, view=None)
                    else:
                        embed = discord.Embed(title=f"Opinia #{numer} • {review['rating']}/5", description=safe(review['comment'], 900) or 'Bez komentarza')
                        embed.add_field(name='Sprzedawca', value=f"<@{review['seller']}>")
                        embed.add_field(name='Kupujący', value=f"<@{review['buyer']}>")
                        await msg.edit(content=None, embed=embed, view=buttons(('profile', review['seller'], 'Profil sprzedawcy')))
                except discord.NotFound:
                    pass
            await self.audit(i.user.id, 'review_hidden', {'review': numer, 'hidden': ukryj, 'reason': powod})
            await self.reply(i, 'Zapisano decyzję. Ukryte opinie nie wchodzą do średniej.')

        @tree.command(name='odpowiedz', description='Sprzedawca: odpowiedz publicznie na opinię')
        @app_commands.guild_only()
        async def answer_cmd(i: discord.Interaction, opinia: int, tresc: app_commands.Range[str, 1, 700]):
            self.check(i)
            review = self.own_record('reviews', opinia)
            if i.user.id != review['seller'] or review['hidden']:
                raise RuleError('Możesz odpowiedzieć tylko na widoczną opinię o Tobie.')
            self.throttle(i.user.id, f'reply_{opinia}', 86400)
            await i.response.defer(ephemeral=True, thinking=True)
            await (await self.channel('reviews')).send(f"**Odpowiedź sprzedawcy <@{i.user.id}> do opinii #{opinia}:**\n{safe(tresc, 700)}")
            await self.audit(i.user.id, 'review_reply', {'review': opinia, 'text': tresc})
            await self.reply(i, 'Odpowiedź opublikowano.')

        @tree.command(name='moderuj', description='Moderator: ostrzeżenie lub czasowe wyciszenie użytkownika')
        @app_commands.guild_only()
        @app_commands.choices(akcja=[app_commands.Choice(name=v, value=k) for k, v in {'warn': 'Ostrzeżenie', 'timeout': 'Wyciszenie', 'untimeout': 'Zdejmij wyciszenie'}.items()])
        async def moderation_cmd(i: discord.Interaction, osoba: discord.Member, akcja: str, powod: app_commands.Range[str, 5, 400], minuty: app_commands.Range[int, 1, 40320] = 60):
            self.require_staff(i, 'mod')
            if osoba.id == i.user.id or osoba.bot or osoba.id == i.guild.owner_id or osoba.guild_permissions.administrator:
                raise RuleError('Nie można wykonać tej czynności wobec tej osoby.')
            if osoba.top_role >= i.guild.me.top_role or (i.user.id != i.guild.owner_id and osoba.top_role >= i.user.top_role):
                raise RuleError('Nie możesz moderować osoby z równą lub wyższą rolą.')
            await i.response.defer(ephemeral=True, thinking=True)
            if akcja == 'timeout':
                await osoba.timeout(timedelta(minutes=minuty), reason=powod)
            elif akcja == 'untimeout':
                await osoba.timeout(None, reason=powod)
            await self.audit(i.user.id, 'moderation_' + akcja, {'user': osoba.id, 'reason': powod, 'minutes': minuty})
            await self.reply(i, 'Działanie zapisano. Ostrzeżenia są dostępne w logach administracji.')

        @tree.command(name='wyczysc', description='Moderator: usuń do 100 ostatnich wiadomości na kanale społeczności')
        @app_commands.guild_only()
        async def clean_cmd(i: discord.Interaction, liczba: app_commands.Range[int, 1, 100]):
            self.require_staff(i, 'mod')
            if i.channel_id not in [self.cfg()['channels'][key] for key in ('chat', 'showcase', 'ideas')]:
                raise RuleError('Czyszczenie jest dostępne tylko na kanałach rozmów, projektów i propozycji.')
            await i.response.defer(ephemeral=True, thinking=True)
            messages = await i.channel.purge(limit=liczba)
            await self.audit(i.user.id, 'messages_deleted', {'channel': i.channel_id, 'count': len(messages)})
            await self.reply(i, f'Usunięto {len(messages)} wiadomości.')

        @tree.command(name='statystyki', description='Administracja: podstawowe statystyki huba')
        @app_commands.guild_only()
        async def stats_cmd(i: discord.Interaction):
            self.require_staff(i, 'admin')
            values = []
            for table in ('offers', 'orders', 'reviews', 'tickets'):
                n = self.db.one(f'SELECT COUNT(*) AS n FROM {table} WHERE guild=?', (self.guild_id,))['n']
                values.append(f'{table}: **{n}**')
            await self.reply(i, '\n'.join(values) + f'\nOpóźnienie połączenia: {round(self.latency * 1000)} ms')

        @tree.command(name='diagnostyka', description='Administracja: sprawdź role, kanały i sprawy po błędach')
        @app_commands.guild_only()
        async def diagnose_cmd(i: discord.Interaction):
            self.require_staff(i, 'admin')
            cfg = self.cfg()
            missing_roles = [key for key, ident in cfg['roles'].items() if not i.guild.get_role(ident)]
            missing_channels = [key for key, ident in cfg['channels'].items() if not i.guild.get_channel(ident)]
            stuck = self.db.rows("SELECT id,state FROM orders WHERE guild=? AND state='creating'", (self.guild_id,))
            offers = self.db.rows("SELECT id FROM offers WHERE guild=? AND status='publishing'", (self.guild_id,))
            await self.reply(i, f"Brak ról: {', '.join(missing_roles) or 'nie'}\nBrak kanałów: {', '.join(missing_channels) or 'nie'}\n"
                f"Zamówienia w tworzeniu: {', '.join(str(o['id']) for o in stuck) or 'brak'}\n"
                f"Oferty do ponownej publikacji: {', '.join(str(o['id']) for o in offers) or 'brak'}\n"
                'Brakującą strukturę dodasz przez /konfiguruj. Publikacja: /oferta → Ponów. Zamówienie w tworzeniu: ponownie /kup; anulowanie przez /zamowienie.')

        @tree.command(name='historia', description='Pobierz tekstową historię bieżącego zamówienia lub zgłoszenia')
        @app_commands.guild_only()
        async def transcript_cmd(i: discord.Interaction):
            self.check(i)
            ch = i.channel
            order = self.db.one('SELECT * FROM orders WHERE guild=? AND channel=?', (self.guild_id, i.channel_id))
            ticket = self.db.one('SELECT * FROM tickets WHERE guild=? AND channel=?', (self.guild_id, i.channel_id))
            allowed = self.is_staff(i.user) or (order and i.user.id in (order['buyer'], order['seller'])) or (ticket and i.user.id == ticket['user'])
            if not (order or ticket) or not allowed:
                raise RuleError('Historii można użyć na własnym kanale zamówienia / zgłoszenia albo jako jego opiekun.')
            await i.response.defer(ephemeral=True, thinking=True)
            records = []
            count = 0
            async for msg in ch.history(limit=2000, oldest_first=True):
                count += 1
                text = f'[{msg.created_at.isoformat()}] {msg.author} ({msg.author.id}): {msg.content}\n'
                for embed in msg.embeds:
                    text += json.dumps(embed.to_dict(), ensure_ascii=False) + '\n'
                for attachment in msg.attachments:
                    text += f'Załącznik: {attachment.filename} ({attachment.url})\n'
                records.append(text)
            header = 'Eksport maks. pierwszych 2000 wiadomości; edycje i usunięte wiadomości nie są odtwarzane. Załączniki jako linki mogą wygasnąć.\n\n'
            data = (header + '\n'.join(records)).encode('utf-8')
            if len(data) > 7 * 1024 * 1024:
                raise RuleError('Historia jest zbyt duża do załączenia. Administrator powinien pobrać ją partiami.')
            await self.reply(i, f'Historia: {count} wiadomości.', file=discord.File(io.BytesIO(data), filename=f'historia-{ch.id}.txt'))

        @tree.command(name='backup', description='Właściciel: pobierz kopię bazy lub eksport ustawień')
        @app_commands.guild_only()
        async def backup_cmd(i: discord.Interaction):
            self.check(i)
            if i.user.id != i.guild.owner_id:
                raise RuleError('Kopię całej bazy pobiera wyłącznie właściciel.')
            await i.response.defer(ephemeral=True, thinking=True)
            path = self.data_dir / 'backup-do-pobrania.sqlite3'
            self.db.backup(path)
            try:
                if path.stat().st_size > min(i.filesize_limit, 7 * 1024 * 1024):
                    await self.reply(i, 'Kopia jest za duża do wysłania. Pobierz pliki z data/backups w panelu hostingu.')
                else:
                    await self.reply(i, 'Kopia zawiera prywatne dane i decyzje administracji. Nie publikuj jej.', file=discord.File(path, filename='hub-backup.sqlite3'))
            finally:
                path.unlink(missing_ok=True)

    @tasks.loop(hours=1)
    async def maintenance(self):
        try:
            guild = self.get_guild(self.guild_id)
            if not guild or not self.db.config(self.guild_id).get('channels'):
                return
            # Retencja plików dowodowych: otwarty spór wstrzymuje usunięcie.
            threshold = int(time.time()) - self.settings['proof_retention_days'] * 86400
            proofs = self.db.rows('''SELECT p.*,o.state FROM proofs p JOIN orders o ON o.id=p.order_id
                WHERE o.guild=? AND p.staff_message IS NOT NULL AND p.status!='pending'
                AND p.created<? AND o.state!='disputed' LIMIT 30''', (self.guild_id, threshold))
            for proof in proofs:
                try:
                    ch = await self.channel('proof_queue')
                    message = await ch.fetch_message(proof['staff_message'])
                    await message.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    LOG.warning('Nie udało się usunąć dowodu zgodnie z retencją; ponowię za godzinę.')
                    continue
                self.db.execute('UPDATE proofs SET staff_message=NULL WHERE id=?', (proof['id'],))
            # Kopie obejmują metadane, nie obrazy dowodów; siedem ostatnich plików.
            backup_dir = self.data_dir / 'backups'
            name = time.strftime('hub-%Y-%m-%d.sqlite3', time.gmtime())
            self.db.backup(backup_dir / name)
            for old in sorted(backup_dir.glob('hub-*.sqlite3'))[:-7]:
                old.unlink()
            cfg = self.cfg()
            now = int(time.time())
            if now - cfg.get('last_reminder', 0) > self.settings['reminder_hours'] * 3600:
                waiting = self.db.one("SELECT COUNT(*) AS n FROM orders WHERE guild=? AND (proof_status='pending' OR state='disputed')", (self.guild_id,))['n']
                offers = self.db.one("SELECT COUNT(*) AS n FROM offers WHERE guild=? AND status='pending'", (self.guild_id,))['n']
                if waiting or offers:
                    await (await self.channel('alerts')).send(f'Oczekujące zamówienia / spory: **{waiting}**. Oferty do sprawdzenia: **{offers}**. Użyj /kolejka.')
                cfg['last_reminder'] = now
                self.db.save_config(self.guild_id, cfg)
            self.cooldowns = {key: stamp for key, stamp in self.cooldowns.items() if time.monotonic() - stamp < 86400}
        except Exception:
            LOG.exception('Błąd konserwacji; kolejne uruchomienie za godzinę.')

    @maintenance.before_loop
    async def before_maintenance(self):
        await self.wait_until_ready()
