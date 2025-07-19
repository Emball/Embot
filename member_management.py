# member_management.py
import discord
import logging
from datetime import datetime, timedelta, timezone
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger('member_management')

# Configuration
SUSPICIOUS_THRESHOLD = 50  # Fed score threshold to trigger alerts
FED_DETECTION_CHANNEL = "fed-alerts"  # Channel for fed alerts
OWNER_ROLE_NAME = "Owner"  # Role to notify about feds
RESTRICTED_ROLE_NAME = "Restricted"  # Role for suspected feds
NEWBIE_ROLE_NAME = "Newbie"  # Role for new members
MEMBER_ROLE_NAME = "Member"  # Role for trusted members
LEVEL_THRESHOLD = 2  # Level required to become Member

class MemberManagement:
    def __init__(self, bot):
        self.bot = bot
        self.vindicated_users = set()  # Users cleared of fed suspicion
        logger.info("Member management system initialized")

    async def calculate_fed_score(self, member):
        """Calculate a fed suspicion score for a member"""
        logger.debug(f"Calculating fed score for {member}")
        score = 0
        
        # Get current time in UTC with timezone awareness
        now = datetime.now(timezone.utc)
        
        # Account age vs join time (timezone aware calculations)
        account_age = (now - member.created_at).total_seconds()
        join_delay = (member.joined_at - member.created_at).total_seconds()
        
        if join_delay < 3600:  # Joined within 1 hour of creation
            logger.debug(f"Join delay <1hr: +40 ({join_delay}s)")
            score += 40
        elif join_delay < 86400:  # Joined within 24 hours
            logger.debug(f"Join delay <24hr: +20 ({join_delay}s)")
            score += 20
            
        # Profile indicators
        if member.default_avatar:
            logger.debug("Default avatar detected: +20")
            score += 20
            
        try:
            user = await self.bot.fetch_user(member.id)
            if not user.banner:
                logger.debug("No banner detected: +10")
                score += 10
            if not user.public_flags.premium:
                logger.debug("No nitro detected: +10")
                score += 10
        except:
            logger.warning(f"Couldn't fetch user details for {member.id}: +10")
            score += 10  # Assume suspicious if we can't verify
            
        # Other indicators
        if len(member.roles) == 1:  # Only @everyone role
            logger.debug("Only @everyone role: +10")
            score += 10
            
        logger.info(f"Fed score for {member}: {score}/100")
        return score

    async def handle_new_member(self, member):
        """Handle new member joining the server"""
        logger.info(f"New member joined: {member}")
        try:
            # Assign Newbie role
            newbie_role = discord.utils.get(member.guild.roles, name=NEWBIE_ROLE_NAME)
            if newbie_role:
                await member.add_roles(newbie_role)
                logger.info(f"Assigned Newbie role to {member}")
            else:
                logger.warning(f"Newbie role not found for {member}")
            
            # Skip fed check if vindicated
            if member.id in self.vindicated_users:
                logger.info(f"Skipping fed check for vindicated user: {member}")
                return
                
            # Calculate fed score
            fed_score = await self.calculate_fed_score(member)
            
            if fed_score >= SUSPICIOUS_THRESHOLD:
                logger.info(f"Suspicious fed score detected: {fed_score} >= {SUSPICIOUS_THRESHOLD}")
                await self.handle_suspicious_member(member, fed_score)
            else:
                logger.info(f"Fed score below threshold: {fed_score} < {SUSPICIOUS_THRESHOLD}")
        except Exception as e:
            logger.error(f"Error handling new member {member}: {e}")

    async def handle_suspicious_member(self, member, fed_score):
        """Handle a member flagged as suspicious"""
        logger.info(f"Handling suspicious member: {member} (Score: {fed_score})")
        try:
            # Assign restricted role
            restricted_role = discord.utils.get(member.guild.roles, name=RESTRICTED_ROLE_NAME)
            if restricted_role:
                await member.add_roles(restricted_role)
                logger.info(f"Assigned Restricted role to {member}")
            else:
                logger.error(f"Restricted role not found for {member}")
            
            # Send DM to the member
            try:
                logger.debug(f"Sending DM to suspicious member: {member}")
                embed = discord.Embed(
                    title="🚨 SECURITY ALERT 🚨",
                    description="Our systems have detected suspicious activity on your account!",
                    color=discord.Color.red()
                )
                embed.add_field(
                    name="Nice try, f@ggot!",
                    value="Our security team has been notified. "
                          "You'll be restricted until an owner reviews your case.",
                    inline=False
                )
                # Use a real banner URL instead of the placeholder
                embed.set_image(url="https://i.imgur.com/7b0Q3ma.png")
                await member.send(embed=embed)
                logger.info(f"Sent DM to {member}")
            except discord.Forbidden:
                logger.warning(f"Couldn't send DM to {member}")
            except Exception as e:
                logger.error(f"Error sending DM to {member}: {e}")
            
            # Send alert to owners
            await self.send_fed_alert(member, fed_score)
        except Exception as e:
            logger.error(f"Error handling suspicious member {member}: {e}")

    async def send_fed_alert(self, member, fed_score):
        """Send fed alert to owners"""
        logger.info(f"Sending fed alert for {member}")
        try:
            # Find alert channel
            alert_channel = discord.utils.get(member.guild.text_channels, name=FED_DETECTION_CHANNEL)
            if not alert_channel:
                logger.error(f"Fed alert channel '{FED_DETECTION_CHANNEL}' not found")
                # Fallback to bot-logs channel if exists
                alert_channel = discord.utils.get(member.guild.text_channels, name="bot-logs")
                if not alert_channel:
                    return
                
            # Create embed
            embed = discord.Embed(
                title="🚨 POTENTIAL FED DETECTED",
                description=f"@Owner Member {member.mention} has been flagged by our security system",
                color=discord.Color.orange()
            )
            
            # Add account details
            embed.add_field(name="Account Created", value=member.created_at.strftime("%Y-%m-%d %H:%M"), inline=True)
            embed.add_field(name="Joined Server", value=member.joined_at.strftime("%Y-%m-%d %H:%M"), inline=True)
            embed.add_field(name="Fed Score", value=f"{fed_score}/100", inline=False)
            
            # Add indicators
            indicators = []
            if member.default_avatar:
                indicators.append("Default Avatar")
            try:
                user = await self.bot.fetch_user(member.id)
                if not user.banner:
                    indicators.append("No Banner")
                if not user.public_flags.premium:
                    indicators.append("No Nitro")
            except:
                indicators.append("Profile Unavailable")
                
            if len(member.roles) == 1:
                indicators.append("No Additional Roles")
                
            if indicators:
                embed.add_field(name="Suspicious Indicators", value=", ".join(indicators), inline=False)
            
            # Add action buttons
            embed.set_footer(text="Use the buttons below to take action")
            
            # Send message with action buttons
            view = FedActionView(member)
            owner_role = discord.utils.get(member.guild.roles, name=OWNER_ROLE_NAME)
            mention = f"<@&{owner_role.id}>" if owner_role else "@Owners"
            
            await alert_channel.send(
                mention,
                embed=embed,
                view=view
            )
            logger.info(f"Sent fed alert for {member} in #{alert_channel.name}")
        except Exception as e:
            logger.error(f"Error sending fed alert for {member}: {e}")

    async def promote_to_member(self, member):
        """Promote a member from Newbie to Member"""
        logger.info(f"Promoting {member} to Member role")
        try:
            newbie_role = discord.utils.get(member.guild.roles, name=NEWBIE_ROLE_NAME)
            member_role = discord.utils.get(member.guild.roles, name=MEMBER_ROLE_NAME)
            
            if not newbie_role:
                logger.error(f"Newbie role not found for {member}")
                return
            if not member_role:
                logger.error(f"Member role not found for {member}")
                return
                
            if newbie_role in member.roles:
                await member.remove_roles(newbie_role)
                logger.info(f"Removed Newbie role from {member}")
            else:
                logger.warning(f"Newbie role not present on {member}")
                
            if member_role not in member.roles:
                await member.add_roles(member_role)
                logger.info(f"Added Member role to {member}")
            else:
                logger.warning(f"Member role already present on {member}")
        except Exception as e:
            logger.error(f"Error promoting {member}: {e}")

class FedActionView(discord.ui.View):
    """View for handling fed actions"""
    def __init__(self, member):
        super().__init__(timeout=None)
        self.member = member
        logger.debug(f"Created FedActionView for {member}")
        
    @discord.ui.button(label="Vindicate", style=discord.ButtonStyle.success, custom_id="fed_vindicate")
    async def vindicate(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Clear the member of suspicion"""
        logger.info(f"Vindicate action requested for {self.member} by {interaction.user}")
        try:
            # Remove restricted role
            restricted_role = discord.utils.get(interaction.guild.roles, name=RESTRICTED_ROLE_NAME)
            if restricted_role and restricted_role in self.member.roles:
                await self.member.remove_roles(restricted_role)
                logger.info(f"Removed Restricted role from {self.member}")
            elif restricted_role:
                logger.warning(f"Restricted role not present on {self.member}")
                
            # Add to vindicated list
            self.bot.member_management.vindicated_users.add(self.member.id)
            logger.info(f"Added {self.member} to vindicated users")
            
            # Promote if eligible
            stats = self.bot.level_system.get_user_stats(self.member.id)
            if stats and stats["level"] >= LEVEL_THRESHOLD:
                logger.info(f"Promoting vindicated member {self.member} to Member")
                await self.bot.member_management.promote_to_member(self.member)
            
            await interaction.response.send_message(
                f"✅ {self.member.mention} has been vindicated! Restrictions removed.",
                ephemeral=True
            )
            logger.info(f"Vindicated {self.member} successfully")
        except Exception as e:
            logger.error(f"Vindicate error for {self.member}: {e}")
            await interaction.response.send_message(
                "❌ Failed to vindicate user",
                ephemeral=True
            )
            
    @discord.ui.button(label="Restrict", style=discord.ButtonStyle.secondary, custom_id="fed_restrict")
    async def restrict(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Keep member restricted"""
        logger.info(f"Restrict action requested for {self.member} by {interaction.user}")
        try:
            # Ensure restricted role is applied
            restricted_role = discord.utils.get(interaction.guild.roles, name=RESTRICTED_ROLE_NAME)
            if restricted_role and restricted_role not in self.member.roles:
                await self.member.add_roles(restricted_role)
                logger.info(f"Added Restricted role to {self.member}")
            elif restricted_role:
                logger.info(f"Restricted role already present on {self.member}")
                
            await interaction.response.send_message(
                f"🔒 {self.member.mention} will remain restricted.",
                ephemeral=True
            )
            logger.info(f"Restricted {self.member} successfully")
        except Exception as e:
            logger.error(f"Restrict error for {self.member}: {e}")
            await interaction.response.send_message(
                "❌ Failed to restrict user",
                ephemeral=True
            )
            
    @discord.ui.button(label="Ban", style=discord.ButtonStyle.danger, custom_id="fed_ban")
    async def ban(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Ban the suspected fed"""
        logger.info(f"Ban action requested for {self.member} by {interaction.user}")
        try:
            await self.member.ban(reason="Fed detection system")
            await interaction.response.send_message(
                f"⛔ {self.member.mention} has been banned.",
                ephemeral=True
            )
            logger.info(f"Banned {self.member} successfully")
        except Exception as e:
            logger.error(f"Ban error for {self.member}: {e}")
            await interaction.response.send_message(
                "❌ Failed to ban user",
                ephemeral=True
            )

def setup(bot):  # Changed from setup_member_management
    """Setup member management commands and events"""
    logger.info("Initializing member management system")
    try:
        mgmt = MemberManagement(bot)
        bot.member_management = mgmt

        @bot.event
        async def on_member_join(member):
            """Handle new members joining"""
            logger.info(f"Member joined: {member}")
            await mgmt.handle_new_member(member)

        @bot.event
        async def on_level_up(user_id, old_level, new_level, message):
            """Handle level ups for role promotion"""
            logger.info(f"Level up detected: {user_id} (L{old_level}→L{new_level})")
            try:
                # Check if user reached member threshold
                if new_level >= LEVEL_THRESHOLD:
                    member = message.guild.get_member(user_id)
                    if member:
                        # Skip if user is restricted or vindicated
                        restricted_role = discord.utils.get(member.guild.roles, name=RESTRICTED_ROLE_NAME)
                        if restricted_role and restricted_role in member.roles:
                            logger.info(f"Skipping promotion for restricted member: {member}")
                            return
                        
                        # Check if user has Newbie role
                        newbie_role = discord.utils.get(member.guild.roles, name=NEWBIE_ROLE_NAME)
                        if newbie_role and newbie_role in member.roles:
                            logger.info(f"Promoting {member} to Member role after level up")
                            await mgmt.promote_to_member(member)
                        else:
                            logger.debug(f"No Newbie role found on {member}")
                    else:
                        logger.warning(f"Member not found for level up: {user_id}")
                else:
                    logger.debug(f"Level {new_level} below threshold ({LEVEL_THRESHOLD}) for {user_id}")
            except Exception as e:
                logger.error(f"Level up role promotion error: {e}")

        logger.info("Member management system initialized successfully")
    except Exception as e:
        logger.critical(f"Failed to initialize member management: {e}")
        raise