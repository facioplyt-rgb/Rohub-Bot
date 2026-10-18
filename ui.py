"""Trwałe przyciski: identyfikatory są odtwarzane po restarcie procesu."""
import discord


class Action(discord.ui.DynamicItem[discord.ui.Button], template=r'hub:(?P<action>[a-z_]+):(?P<id>[0-9]+)'):
    def __init__(self, action, ident=0, label='Otwórz', style=discord.ButtonStyle.secondary):
        self.action = action
        self.ident = int(ident)
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f'hub:{action}:{ident}'))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match['action'], int(match['id']), item.label or 'Otwórz', item.style)

    async def callback(self, interaction):
        try:
            await interaction.client.route(interaction, self.action, self.ident)
        except Exception as error:
            await interaction.client.handle_error(interaction, error)


def buttons(*items):
    view = discord.ui.View(timeout=None)
    for item in items:
        if len(item) == 2:
            view.add_item(Action(item[0], 0, item[1]))
        else:
            view.add_item(Action(*item))
    return view


class Form(discord.ui.Modal):
    def __init__(self, title, fields, callback):
        super().__init__(title=title[:45], timeout=600)
        self.handler = callback
        self.inputs = {}
        for key, label, maximum, paragraph, required, default in fields:
            item = discord.ui.TextInput(label=label[:45], max_length=maximum, required=required,
                style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
                default=default or None)
            self.inputs[key] = item
            self.add_item(item)

    async def on_submit(self, interaction):
        await self.handler(interaction, {key: value.value.strip() for key, value in self.inputs.items()})

    async def on_error(self, interaction, error):
        await interaction.client.handle_error(interaction, error)


def field(key, label, maximum=500, paragraph=True, required=True, default=''):
    return key, label, maximum, paragraph, required, default
