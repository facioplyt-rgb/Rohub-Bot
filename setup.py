"""Dodająca, powtarzalna konfiguracja. Nie usuwa istniejących kanałów ani ról."""
import discord
from .store import RuleError

ROLE_NAMES = {
    'admin': 'Hub • Administrator', 'mod': 'Hub • Moderator',
    'reviewer': 'Hub • Weryfikator', 'trade': 'Hub • Opiekun transakcji',
    'seller': 'Hub • Sprzedawca', 'trusted': 'Hub • Sprawdzony sprzedawca',
    'customer': 'Hub • Klient', 'member': 'Hub • Użytkownik',
}

CATEGORIES = {
    'start': '📌 START', 'panel': '🛒 PANEL HUBA', 'official': '✅ OFERTY OFICJALNE',
    'community': '🏷️ OFERTY SPOŁECZNOŚCI', 'social': '💬 SPOŁECZNOŚĆ',
    'orders': '📦 ZAMÓWIENIA', 'archive': '🗃️ ARCHIWUM',
    'support': '🎫 ZGŁOSZENIA', 'staff': '🔐 ADMINISTRACJA',
}

TEXT_CHANNELS = [
    ('welcome', 'start', '👋・powitanie'), ('rules', 'start', '📜・regulamin'),
    ('howto', 'start', '📖・jak-korzystać'), ('news', 'start', '📢・ogłoszenia'),
    ('buyer_panel', 'panel', '🛒・panel-kupującego'),
    ('seller_panel', 'panel', '🏷️・panel-sprzedawcy'),
    ('search', 'panel', '🔎・szukaj-ofert'), ('ranking', 'panel', '🏆・ranking'),
    ('chat', 'social', '💬・rozmowy'), ('showcase', 'social', '🎨・wasze-projekty'),
    ('ideas', 'social', '💡・propozycje'), ('reviews', 'social', '⭐・opinie'),
    ('help', 'social', '🎫・pomoc'),
    ('offer_queue', 'staff', '📋・weryfikacja-ofert'),
    ('proof_queue', 'staff', '🧾・dowody-zakupów'),
    ('reports', 'staff', '🚩・zgłoszenia'), ('logs', 'staff', '📚・logi'),
    ('alerts', 'staff', '⚙️・stan-bota'),
]


def bot_permissions():
    return discord.Permissions(
        view_channel=True, send_messages=True, read_message_history=True,
        embed_links=True, attach_files=True, manage_channels=True,
        manage_roles=True, manage_messages=True, manage_threads=True,
        create_public_threads=True, send_messages_in_threads=True,
        moderate_members=True,
    )


async def configure(client, guild):
    if 'COMMUNITY' not in guild.features:
        raise RuleError('Najpierw włącz Społeczność / Community w ustawieniach serwera. Instrukcja jest w PORADNIK.html.')
    me = guild.me
    required = bot_permissions()
    missing = [name for name, value in required if value and not getattr(me.guild_permissions, name)]
    if missing:
        raise RuleError('Botowi brakuje uprawnień: ' + ', '.join(missing))
    cfg = client.db.config(guild.id) or {'roles': {}, 'channels': {}, 'categories': {}, 'panels': {}}
    for key in ('roles', 'channels', 'categories', 'panels'):
        cfg.setdefault(key, {})

    def save():
        client.db.save_config(guild.id, cfg)

    for key, name in ROLE_NAMES.items():
        role = guild.get_role(cfg['roles'].get(key, 0))
        if not role:
            role = await guild.create_role(name=name, permissions=discord.Permissions.none(),
                                           colour=discord.Colour.blurple() if key in ('admin', 'seller') else discord.Colour.default(),
                                           reason='Konfiguracja huba na polecenie właściciela')
            cfg['roles'][key] = role.id
            save()

    roles = {key: guild.get_role(value) for key, value in cfg['roles'].items()}
    bot_allow = discord.PermissionOverwrite(view_channel=True, send_messages=True,
        read_message_history=True, embed_links=True, attach_files=True,
        manage_channels=True, manage_messages=True, manage_threads=True,
        create_public_threads=True, send_messages_in_threads=True)
    read_only = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False,
            send_messages_in_threads=False, add_reactions=False),
        me: bot_allow,
    }
    for key in ('admin',):
        read_only[roles[key]] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                          send_messages_in_threads=True)
    private = {guild.default_role: discord.PermissionOverwrite(view_channel=False), me: bot_allow}
    for key in ('admin', 'trade'):
        private[roles[key]] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
            read_message_history=True, attach_files=True, embed_links=True)
    staff = dict(private)
    for key in ('reviewer', 'mod'):
        staff[roles[key]] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    for key, name in CATEGORIES.items():
        category = guild.get_channel(cfg['categories'].get(key, 0))
        if not category:
            acl = staff if key == 'staff' else private if key in ('orders', 'archive', 'support') else read_only
            category = await guild.create_category(name, overwrites=acl, reason='Konfiguracja huba')
            cfg['categories'][key] = category.id
            save()

    for key, parent, name in TEXT_CHANNELS:
        if guild.get_channel(cfg['channels'].get(key, 0)):
            continue
        acl = dict(read_only)
        if key in ('chat', 'showcase', 'ideas'):
            acl[roles['member']] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                attach_files=True, embed_links=True, add_reactions=True)
        elif key == 'proof_queue':
            acl = dict(private)
        elif parent == 'staff':
            acl = dict(staff)
        channel = await guild.create_text_channel(name, category=guild.get_channel(cfg['categories'][parent]),
            overwrites=acl, slowmode_delay=5 if key in ('chat', 'showcase', 'ideas') else 0,
            topic='Kanał huba. Zarządzany przez bota.', reason='Konfiguracja huba')
        cfg['channels'][key] = channel.id
        save()

    for official in (False, True):
        for category in ('modele', 'mapy', 'skrypty', 'zestawy', 'uslugi'):
            key = f"forum_{'official' if official else 'community'}_{category}"
            if guild.get_channel(cfg['channels'].get(key, 0)):
                continue
            acl = dict(read_only)
            # Posty tworzy wyłącznie bot; pytania i zakup przez przyciski.
            forum = await guild.create_forum(
                f"{'✅' if official else '🏷️'}・{category}",
                category=guild.get_channel(cfg['categories']['official' if official else 'community']),
                overwrites=acl, topic='Jeden produkt = jeden post. Wystawianie przez panel sprzedawcy.',
                available_tags=[discord.ForumTag(name='Dostępne'), discord.ForumTag(name='Wstrzymane')],
                default_layout=discord.ForumLayoutType.gallery_view,
                reason='Konfiguracja katalogu ofert')
            cfg['channels'][key] = forum.id
            save()
    return cfg
