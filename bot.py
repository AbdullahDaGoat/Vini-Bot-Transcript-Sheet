import os
import discord
import asyncio
from enum import Enum
from discord import app_commands
from discord.ext import commands
from datetime import timedelta, timezone
import chat_exporter
import threading
from dotenv import load_dotenv
from flask import Flask, render_template_string
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
URL_ANCHOR = os.getenv("URL")

app = Flask(__name__)

intents = discord.Intents.default()
intents.members = True
vini = commands.Bot(command_prefix='.', intents=discord.Intents.all(), owner_id=1214793054926409738)
vini.remove_command('help')

def get_random_food_gif():
    api_key = os.getenv('GIPHY_API_KEY') 
    url = f"https://api.giphy.com/v1/gifs/random?api_key={api_key}&tag=food&rating=g"
    response = requests.get(url)
    if response.status_code == 200:
        gif_url = response.json()['data']['images']['original']['url']
        return gif_url
    else:
        return None

@app.route('/transcript/<channel_id>')
def transcript(channel_id):
    try:
        with open(f'transcript_{channel_id}.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
    except FileNotFoundError:
        return "Transcript not found", 404

    return render_template_string(html_content)

@vini.event
async def on_ready():
    await vini.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="testing"))
    print(f"Logged on as {vini.user}")
    
    # Start Flask app in a separate thread
    threading.Thread(target=app.run, kwargs={'host': '0.0.0.0', 'port': int(os.environ.get('PORT', 5000))}, daemon=True).start()

@vini.command()
@commands.has_permissions(manage_messages=True)
async def sync(ctx):
    synced = await vini.tree.sync()
    await ctx.send(f"Synced {len(synced)} commands.", delete_after=2)

# Function to convert UTC to EST
def utc_to_est(utc_dt):
    est = timezone(timedelta(hours=-5))  # Eastern Standard Time (EST) with UTC-5
    est_dt = utc_dt.replace(tzinfo=timezone.utc).astimezone(est)
    return est_dt

async def get_transcript(channel: discord.TextChannel, private_channel_id: int):
    export = await chat_exporter.export(channel=channel)
    if export is None:
        return None
        
    file_name = f"transcript_{channel.id}.html"
    with open(file_name, "w", encoding="utf-8") as f:
        f.write(export)
    
    export = export.replace("UTC", "EST")  # Convert timestamps
    
    # Create the URL
    transcript_url = f"http://{URL_ANCHOR}/transcript/{channel.id}"

    private_channel = channel.guild.get_channel(private_channel_id)
    if private_channel:
        # Split the message if it's too long
        message_content = f"Transcript URL: {transcript_url}\nRaw HTML: {export}"
        if len(message_content) > 4000:
            # Sending the URL and a truncated message
            await private_channel.send(f"Transcript URL: {transcript_url}")
            await private_channel.send(f"Raw HTML is too long to display here. You can download it from the transcript link.")
        else:
            await private_channel.send(message_content)
        return transcript_url
    else:
        print(f"Private channel with ID {private_channel_id} not found.")
        return None

class Shifts(Enum):
    # format: name - value
    Open = "open"
    Close = "close"
    Pause = "pause"

# Define the Regions Enum
class Regions(Enum):
    Both = "both"
    USA = "usa"
    Canada = "canada"

# Dictionary to keep track of region statuses
region_status = {
    "USA": "Closed",
    "Canada": "Closed"
}

# Define the TicketDropdown class
class TicketDropdown(discord.ui.Select):
    def __init__(self, region_state):
        self.region_state = region_state
        options = [
            discord.SelectOption(label="Pickup", description="Create a pickup ticket", emoji="🏃"),
            discord.SelectOption(label="Delivery", description="Create a delivery ticket", emoji="🚛"),
            discord.SelectOption(label="Canada Delivery", description="Create a Canada delivery ticket", emoji="🍁")
        ]
        super().__init__(placeholder="Choose a ticket type...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        role3 = guild.get_role(1261582763128000552)  # food
        role4 = guild.get_role(1261582838592045098) # Canada
        role5 = guild.get_role(1261582850952794155) # USA

        # Check the region state and send appropriate ephemeral messages
        if self.region_state == Regions.USA and self.values[0] == "Canada Delivery":
            await interaction.response.send_message("Canada is closed at the moment, we will open as soon as we can. You can open a support ticket or DM a Supervisor for more assistance.", ephemeral=True)
            return
        elif self.region_state == Regions.Canada and (self.values[0] == "Pickup" or self.values[0] == "Delivery"):
            await interaction.response.send_message("Our USA services are currently closed at the moment, we will open as soon as we can. You can open a support ticket or DM a Supervisor for more assistance.", ephemeral=True)
            return

        # Handle ticket creation based on the selected option
        if self.values[0] == "Pickup":
            ticket_name = f"pickup-{interaction.user.name}"
            category_name = "food-open"
            modal = ticket_pickup()
        elif self.values[0] == "Delivery":
            ticket_name = f"deliver-{interaction.user.name}"
            category_name = "food-open"
            modal = ticket_delivery()
        elif self.values[0] == "Canada Delivery":
            ticket_name = f"ca-deliver-{interaction.user.name}"
            category_name = "food-open"
            modal = ticket_canada()

        ticket = discord.utils.get(interaction.guild.channels, name=ticket_name)
        category = discord.utils.get(interaction.guild.categories, name=category_name)

        if ticket is not None:
            await interaction.response.send_message(f"You already have a ticket open at {ticket.mention}!", ephemeral=True)
        else:
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(view_channel=True),
                interaction.guild.me: discord.PermissionOverwrite(view_channel=True),
                role3: discord.PermissionOverwrite(view_channel=True)
            }
            await interaction.response.send_modal(modal)
            await modal.wait()

# Define the DropdownView class
class DropdownView(discord.ui.View):
    def __init__(self, region_state):
        super().__init__(timeout=None)
        self.add_item(TicketDropdown(region_state))

class TicketOptions(discord.ui.View):
    def __init__(self, channel, completed_by):
        super().__init__(timeout=None)
        self.claimed = False  # Track if the ticket has been claimed
        self.claimer = None
        self.reclaimer = None
        self.channel = channel  # Store the channel where the ticket was created
        self.completed_by = completed_by  # Store the user who completed the modal
        self.reminder_task = None  # To store the reminder loop task
        self.ticket_completed = False

        # Remove Reclaim button initially
        self.remove_item(self.reclaim_button)
        self.remove_item(self.yes_button)
        self.remove_item(self.no_button)

        # Start the reminder loop
        self.start_reminder_loop()

    def start_reminder_loop(self):
        async def send_reminders():
            while True:
                await asyncio.sleep(1800)  # Sleep for 10 minutes
                if self.claimer:
                    await self.channel.send(f"```Please remember to complete or cancel the ticket if you're done with your order.```{self.completed_by.mention}", delete_after = 300)

        self.reminder_task = asyncio.create_task(send_reminders())

    async def stop_reminder_loop(self):
        if self.reminder_task:
            self.reminder_task.cancel()
            try:
                await self.reminder_task
            except asyncio.CancelledError:
                pass


    async def is_reclaimer(self, interaction: discord.Interaction):
        return interaction.user == self.reclaimer if self.reclaimer else False

    async def is_supervisor(self, interaction: discord.Interaction):
        # Check if the user has the supervisor role
        guild = interaction.guild
        supervisor_role_id = 1261582732535009291  # Replace with the actual supervisor role ID
        supervisor_role = guild.get_role(supervisor_role_id)
        return supervisor_role in interaction.user.roles

    # Define a method to mark the ticket as completed
    def mark_ticket_completed(self):
        self.ticket_completed = True

    # Define a method to check if the ticket is completed
    def is_ticket_completed(self):
        return self.ticket_completed

    @discord.ui.button(label="Complete", style=discord.ButtonStyle.success, emoji="✅")
    async def complete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimer:
            mention = self.claimer.mention if self.claimer else self.reclaimer.mention
            embed = discord.Embed(title="Ticket Completion", description="Has the ticket been completed?", color=0x00ff00)
            embed.description = f'Only {mention} can confirm the finished ticket.'
            embed.set_footer(text="Please do not complete an order if an order was not finished.")
            view1 = discord.ui.View(timeout=None)
            view1.add_item(self.yes_button)
            view1.add_item(self.no_button)
            await interaction.response.send_message(embed=embed, view=view1)
            await self.stop_reminder_loop()
        else:
            await interaction.response.send_message("You cannot complete the ticket until it is claimed.")

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, emoji="🛎️")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        new_cat_name = "food-claimed"
        new_cat = discord.utils.get(interaction.guild.categories, name=new_cat_name)
        required_role_id = 1261582732535009291 # attendant
        attendant = guild.get_role(1261582732535009291)
        required_role = interaction.guild.get_role(required_role_id)
    
        if required_role not in interaction.user.roles:
            # Send an error message if the user doesn't have the required role
            await interaction.response.send_message(f"The claim button is for {attendant.mention}", ephemeral=True)
            return
        self.claimer = interaction.user
        # Proceed with claiming the ticket if the user has the required role
        self.remove_item(self.claim_button)
        self.add_item(self.reclaim_button)
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"{interaction.user.mention} will now be serving you.")
        await interaction.channel.edit(category=new_cat)

    @discord.ui.button(label="Reclaim", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def reclaim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = vini.get_guild(1261067418219057173)
        role = guild.get_role(710608801069400156) # attendant
        attendant = guild.get_role(1261067501790302249)
        if role not in interaction.user.roles:
            await interaction.response.send_message(f"The reclaim button is for {attendant.mention}", ephemeral=True)
            return
        
        self.claimer = interaction.user
        await interaction.response.send_message(f"{interaction.user.mention} has reclaimed the ticket.")

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        
        if await self.is_reclaimer(interaction) or await self.is_supervisor(interaction):
            modal = completion(initiator=self.completed_by.name, channel=self.channel, interaction=interaction)
            await interaction.response.send_modal(modal)
            await modal.wait()
            self.mark_ticket_completed()
        elif self.claimer == interaction.user:
            modal = completion(initiator=self.completed_by.name, channel=self.channel, interaction=interaction)
            await interaction.response.send_modal(modal)
            await modal.wait()
            self.mark_ticket_completed()
        else:
            await interaction.response.send_message("You are not authorized to complete this ticket.", ephemeral=True)

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self.is_reclaimer(interaction) or await self.is_supervisor(interaction):
            await interaction.response.send_message(f"Unfortantely, your ticket completion was **revoked** {self.completed_by.mention}. Please `close` your ticket or `continute` with your order. Thank you.")
            await interaction.message.delete()
        elif self.claimer == interaction.user:
            await interaction.response.send_message(f"Unfortantely, your ticket completion was **revoked** {self.completed_by.mention}. Please `close` your ticket or `continute` with your order. Thank you.")
            await interaction.message.delete()
        else:
            await interaction.response.send_message("You are not authorized to complete this ticket.", ephemeral=True)

@vini.tree.command(name="food", description="Open or close a food ticket")
@app_commands.checks.has_any_role('Food')
async def food(interaction: discord.Interaction, shifts: Shifts, region: Regions):

    channel_id = 1261582663337250868  # Replace this with the ID of the channel where messages should be deleted

    if interaction.channel_id != channel_id:
        # If the command is not sent in the designated channel, send an ephemeral message
        await interaction.response.send_message(content='Please use the /food command in <#1261069842195288215>.', ephemeral=True)
        return

    # Defer the initial response to avoid timeout
    await interaction.response.defer()

    gif_url = get_random_food_gif()
    
    try:
        # Save the ID of the defer message
        defer_message = await interaction.original_response()
        defer_message_id = defer_message.id

        # Purge all messages except the defer message
        async for message in interaction.channel.history(limit=100):
            if message.id != defer_message_id:  # Exclude the defer message
                await message.delete()
    except discord.Forbidden:
        await interaction.followup.send(content="I don't have permission to delete messages.", ephemeral=True)
        return
    except Exception as e:
        print(f"Error deleting messages: {e}")

    if shifts == Shifts.Open:
        embed = discord.Embed(colour=0x0cc05f)
        new_channel_name = '🍔┃food-open'
        # Update region status and append message to embed based on region
        if region == Regions.USA:
            embed.set_image(url=gif_url)
            region_status["USA"] = "Open"
            region_status["Canada"] = "Closed"
            embed.description = f'# We Are Open \n Kindly select the appropriate dropdown to place your order. If you need assistance, simply contact a Supervisor. \n \n `USA` is **Open** 🟢 \n `Canada` is **Closed** 🔴'
        elif region == Regions.Canada:
            embed.set_image(url=gif_url)
            region_status["Canada"] = "Open"
            region_status["USA"] = "Closed"
            embed.description = f'# We Are Open \n Kindly select the appropriate dropdown to place your order. If you need assistance, simply contact a Supervisor. \n \n `USA` is **Closed** 🔴 \n `Canada` is **Open** 🟢'
        elif region == Regions.Both:
            embed.set_image(url=gif_url)
            region_status["USA"] = "Open"
            region_status["Canada"] = "Open"
            embed.description = f'# We Are Open \n Kindly select the appropriate dropdown to place your order. If you need assistance, simply contact a Supervisor. \n \n `USA` is **Open** 🟢 \n `Canada` is **Open** 🟢'
        embed.set_footer(text='SavingsHub')

        await interaction.followup.send(embed=embed, view=DropdownView(region_state=region))

    elif shifts == Shifts.Close:
        embed = discord.Embed(colour=0xFF0000)
        embed.description = f'# We Are Closed \n \n We will reopen in the morning as soon as an attendant is available. Your patience is greatly appreciated.'
        embed.set_footer(text='SavingsHub')
        await interaction.followup.send(embed=embed)
        new_channel_name = '🔴┃food-closed'

    elif shifts == Shifts.Pause:
        embed = discord.Embed(colour=0xFFA500)
        embed.description = f"# We Are Paused \n \n Currently, all our attendants are away or on break. Please bear with us, and we'll reopen as soon as possible."
        embed.set_footer(text='SavingsHub')
        await interaction.followup.send(embed=embed)
        new_channel_name = '⏸️┃food-paused'
    
    await interaction.channel.edit(name=new_channel_name)

@food.error
async def food_error(ctx, error):
    if isinstance(error, app_commands.MissingAnyRole):
        await ctx.response.send_message(content='You cannot use this command.', ephemeral=True)
    else:
        raise error

class Buttons(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

class ticket_pickup(discord.ui.Modal, title='Pickup Information'):   
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    name = discord.ui.TextInput(
        label="FULL NAME",
        max_length=0,
        min_length=1,
        required=True,
        placeholder="Your answer",
    )
    food = discord.ui.TextInput(
        label="RESTAURANT NAME",
        max_length=0,
        min_length=1,
        required=True,
        placeholder="Your answer",
    )
    addy = discord.ui.TextInput(
        label="RESTAURANT ADDRESS",
        max_length=0,
        min_length=1,
        required=True,
        placeholder="Your answer",
    )
    pay = discord.ui.TextInput(
        label="PAYMENT METHODS",
        max_length=0,
        min_length=1,
        required=True,
        placeholder="Your payment methods",
    )
    info = discord.ui.TextInput(
        label="ADDITIONAL INFORMATION",
        max_length=0,
        min_length=1,
        required=False,
        placeholder="Your answer",
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        role2 = guild.get_role(1261582732535009291)  # Attendant Team
        role3 = guild.get_role(1261582763128000552) # food
        role4 = guild.get_role(1261582838592045098) # Canada
        role5 = guild.get_role(1261582850952794155) # USA
        category = discord.utils.get(guild.categories, name="food-open")
        additional_info = self.info.value or "None"
        # Create the ticket channel
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True),
            guild.me: discord.PermissionOverwrite(view_channel=True),
            role3: discord.PermissionOverwrite(view_channel=False),
            role5: discord.PermissionOverwrite(view_channel=True)
        }
        channel = await guild.create_text_channel(f'pickup-{interaction.user.name}', category=category, overwrites=overwrites)
        ticket_options = TicketOptions(channel, interaction.user)
        
        # Send the message to the ticket channel
        embed1 = discord.Embed(colour=0x0cc05f)
        embed1.set_author(name=f"Pickup Order for {interaction.user.name}")
        embed1.description = f"\n **{self.name.label}** \n ```{self.name}``` \n \n **{self.food.label}** \n ```{self.food}``` \n \n **{self.addy.label}** \n ```{self.addy}``` \n \n **{self.pay.label}** \n ```{self.pay}``` \n \n **{self.info.label}** \n ```{additional_info}```"
        embed1.set_image(url="https://cdn.discordapp.com/attachments/1073363144786849895/1221705101299945543/Pickup_SavingsHub_1.png?ex=66138c8a&is=6601178a&hm=7867c7be264de0a517baf317c9e13c13bf3aa68f4df94549ac565380552ca700&")
        embed1.set_footer(text="Ticket Information", icon_url=interaction.user.display_avatar)
        await interaction.response.send_message(f"{channel.mention}", ephemeral=True, delete_after = 10)
        await channel.send(embed=embed1, view=TicketOptions(channel, interaction.user))
        await channel.send(f"{interaction.user.mention} {role3.mention}", delete_after = 1)
        await channel.send(f"Hello **{interaction.user}**, thank you for choosing `SavingsHub`.\n\nAn **Attendant** will be with you shortly")

class ticket_delivery(discord.ui.Modal, title='Delivery Information'):   
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    name = discord.ui.TextInput(
        label="NAME & ADDRESS",
        max_length=0,
        min_length=1,
        required=True,
        placeholder="Your answer",
    )
    food = discord.ui.TextInput(
        label="RESTAURANT NAME",
        max_length=0,
        min_length=1,
        required=True,
        placeholder="Your answer",
    )
    pay = discord.ui.TextInput(
        label="PAYMENT METHODS",
        max_length=0,
        min_length=1,
        required=True,
        placeholder="Your payment methods",
    )
    tip = discord.ui.TextInput(
        label="TIP FOR DRIVER",
        max_length=2,
        min_length=1,
        required=False,
        placeholder="Your answer",
    )
    info = discord.ui.TextInput(
        label="PHONE NUMBER",
        max_length=0,
        min_length=1,
        required=False,
        placeholder="Your answer",
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        role2 = guild.get_role(1261582732535009291)  # Attendant Team
        role3 = guild.get_role(1261582763128000552) # food
        role4 = guild.get_role(1261582838592045098) # Canada
        role5 = guild.get_role(1261582850952794155) # USA
        category = discord.utils.get(guild.categories, name="food-open")
        additional_info = self.info.value or "None"
        tips = self.tip.value or "0"
        
        # Create the ticket channel
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True),
            guild.me: discord.PermissionOverwrite(view_channel=True),
            role3: discord.PermissionOverwrite(view_channel=False),
            role5: discord.PermissionOverwrite(view_channel=True)
        }
        channel = await guild.create_text_channel(f'deliver-{interaction.user.name}', category=category, overwrites=overwrites)
        ticket_options = TicketOptions(channel, interaction.user)
        
        # Send the message to the ticket channel
        embed1 = discord.Embed(colour=0x0cc05f)
        embed1.set_author(name=f"Delivery Order for {interaction.user.name}")
        embed1.description = f"\n **{self.name.label}** \n ```{self.name}``` \n \n **{self.food.label}** \n ```{self.food}``` \n \n **{self.pay.label}** \n ```{self.pay}``` \n \n **{self.tip.label}** \n ```{tips}``` \n \n **{self.info.label}** \n ```{additional_info}```"
        embed1.set_footer(text="Ticket Information", icon_url=interaction.user.display_avatar)
        embed1.set_image(url="https://cdn.discordapp.com/attachments/1073363144786849895/1221705101685555211/Delivery_SavingsHub.png?ex=66138c8a&is=6601178a&hm=92a54c66ed71b0591df0ec0eeff01b4029bf599f538510b7b5fbf231545e641d&")
        await interaction.response.send_message(f"{channel.mention}", ephemeral=True, delete_after = 10)
        await channel.send(embed=embed1, view=TicketOptions(channel, interaction.user))
        await channel.send(f"{interaction.user.mention} {role2.mention}", delete_after = 1)
        await channel.send(f"Hello **{interaction.user}**, thank you for choosing `SavingsHub`.\n\nAn **Attendant** will be with you shortly")

class ticket_canada(discord.ui.Modal, title='Canada Delivery Information'):   
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    name = discord.ui.TextInput(
        label="NAME & ADDRESS (INCLUDE POSTAL CODE & CITY)",
        max_length=0,
        min_length=1,
        required=True,
        placeholder="Your answer",
    )
    food = discord.ui.TextInput(
        label="RESTAURANT NAME",
        max_length=0,
        min_length=1,
        required=True,
        placeholder="Your answer",
    )
    pay = discord.ui.TextInput(
        label="PAYMENT METHODS",
        max_length=0,
        min_length=1,
        required=True,
        placeholder="Your payment methods",
    )
    tip = discord.ui.TextInput(
        label="TIP FOR DRIVER",
        max_length=2,
        min_length=1,
        required=False,
        placeholder="Your answer",
    )
    info = discord.ui.TextInput(
        label="PHONE NUMBER",
        max_length=0,
        min_length=1,
        required=False,
        placeholder="Your answer",
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        role2 = guild.get_role(1261582732535009291)  # Attendant Team
        role3 = guild.get_role(1261582763128000552) # food
        role4 = guild.get_role(1261582838592045098) # Canada
        role5 = guild.get_role(1261582850952794155) # USA
        category = discord.utils.get(guild.categories, name="food-open")
        additional_info = self.info.value or "None"
        tips = self.tip.value or "0"
        
        # Create the ticket channel
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True),
            guild.me: discord.PermissionOverwrite(view_channel=True),
            role3: discord.PermissionOverwrite(view_channel=False),
            role4: discord.PermissionOverwrite(view_channel=True)
        }
        channel = await guild.create_text_channel(f'ca-deliver-{interaction.user.name}', category=category, overwrites=overwrites)
        ticket_options = TicketOptions(channel, interaction.user)
        
        # Send the message to the ticket channel
        embed1 = discord.Embed(colour=0xD22B2B)
        embed1.set_author(name=f"Canada Delivery Order for {interaction.user.name}")
        embed1.description = f"\n **{self.name.label}** \n ```{self.name}``` \n \n **{self.food.label}** \n ```{self.food}``` \n \n **{self.pay.label}** \n ```{self.pay}``` \n \n **{self.tip.label}** \n ```{tips}``` \n \n **{self.info.label}** \n ```{additional_info}```"
        embed1.set_footer(text="Ticket Information", icon_url=interaction.user.display_avatar)
        embed1.set_image(url="https://cdn.discordapp.com/attachments/1168655822063149138/1251220728418664488/CanadaUberEats_1.png?ex=666dc99c&is=666c781c&hm=6b83f84f9932b9e395b3cb1592882b5835ca74c6056ea26f9abd2c1924b8db75&")
        await interaction.response.send_message(f"{channel.mention}", ephemeral=True, delete_after = 10)
        await channel.send(embed=embed1, view=TicketOptions(channel, interaction.user))
        await channel.send(f"{interaction.user.mention} {role4.mention}", delete_after = 1)
        await channel.send(f"Hello **{interaction.user}**, thank you for choosing `SavingsHub`.\n\nAn **Attendant** will be with you shortly")

def get_google_sheet(sheet_name):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    credentials = ServiceAccountCredentials.from_json_keyfile_name('./vinixee-f4220b9d53db.json', scope)
    client = gspread.authorize(credentials)
    return client.open(sheet_name).sheet1

# Map user IDs to Google Sheet names
user_sheets = {
    "483084770205499392": "Cripsy Sheet",
    "479139962961526784": "Dal Sheet",
    "1225513625113067614": "Matthew Sheet",
    "925075525570023444": "Splito Sheet",
    "875161585423884319": "Time Sheet",
    "1134392440716017704": "Blash Sheet",
    "1230351684325081171": "DebateMyRoomba Sheet"
}

class completion(discord.ui.Modal, title='Order Price Details'):

    def __init__(self, initiator, channel, interaction, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initiator = initiator
        self.channel = channel
        self.interaction = interaction
        self.referall_input = None

    you = discord.ui.TextInput(
        label="How much did you pay?",
        max_length=0,
        min_length=1,
        required=True,
        placeholder="Your answer",
    )

    customer = discord.ui.TextInput(
        label="How much did the customer pay?",
        max_length=0,
        min_length=1,
        required=False,
        placeholder="Your answer",
    )

    referall = discord.ui.TextInput(
        label="Were they referred?",
        max_length=0,
        min_length=1,
        required=False,
        placeholder="Your answer",
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
            guild = interaction.guild
            new_category_name = "food-closed"
            new_category = discord.utils.get(interaction.guild.categories, name=new_category_name)
            log_channel = discord.utils.get(interaction.guild.channels, name="eod-log")
            customer_amount = float(self.customer.value)
            your_amount = float(self.you.value)
            reimbursement = round((customer_amount - your_amount) * 0.20, 2)

            # Send the message to the ticket channel
            embed1 = discord.Embed(colour=0x0cc05f)
            embed1.set_author(name="Pay Info")
            embed1.add_field(name="Attendant Paid", value=f"__**${your_amount}**__", inline=True)
            embed1.add_field(name="Customer Paid", value=f"__**${customer_amount}**__", inline=True)
            embed1.add_field(name="Revenue", value=f"${reimbursement} \n \n {interaction.user.mention} {self.channel.mention}", inline=False)
            embed1.add_field(name="Referral", value=f"\n They were referred by: {self.referall.value}", inline=False)

            # Log data to the respective Google Sheet
            user_id = str(interaction.user.id)
            if user_id in user_sheets:
                sheet_name = user_sheets[user_id]
                sheet = get_google_sheet(sheet_name)
                date_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                # Find the first completely empty row across columns C, D, and E
                col_c_values = sheet.col_values(3)
                col_d_values = sheet.col_values(4)
                col_e_values = sheet.col_values(5)
                
                # Calculate the max of these to find the next available row
                max_length = max(len(col_c_values), len(col_d_values), len(col_e_values))
                
                # Determine the next row; add 1 to get the first empty row
                next_row = max_length + 1

                # Update the cells in the next available row
                sheet.update(range_name=f'C{next_row}:E{next_row}', values=[[date_time, your_amount, customer_amount]])

            # Generate transcript
            transcript_url = await get_transcript(self.channel, 1263292644423897128)
            if transcript_url:
                embed1.add_field(name="Transcript", value=f"[Download Transcript]({transcript_url})", inline=False)
                user = interaction.user
                await user.send(f"Here is your transcript: {transcript_url}")
            else:
                embed1.add_field(name="Transcript", value="No transcript available.", inline=False)

            await log_channel.send(embed=embed1)
            await interaction.channel.edit(category=new_category)
            await interaction.message.delete()
            user = discord.utils.get(interaction.guild.members, name=self.initiator)
            await interaction.followup.send(f"Thank you for using `SavingsHub` {user.mention}. Your ticket will be open for **24 hours** in case you need any other assistance. Have a great day/evening!")
        except ValueError:
            await interaction.followup.send("Please enter a number value. EX: **20.50** or **20**.", ephemeral=True)
if __name__ == "__main__":
    vini.run(DISCORD_TOKEN)

