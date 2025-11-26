import discord
from discord.ext import commands
from discord import app_commands
import sqlite3
import json
import os
from datetime import datetime
from typing import Optional, List, Dict
import asyncio

MODULE_NAME = "PROJECTS"

class ProjectHub:
    """Manages community projects with versioning, voting, and a persistent control center"""
    
    def __init__(self, bot):
        self.bot = bot
        self.db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'projects.db')
        self.config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'projects_config.json')
        self.control_channel_id = None
        self.control_message_id = None
        self.init_database()
        self.load_config()
    
    def load_config(self):
        """Load configuration from file"""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r') as f:
                    config = json.load(f)
                    self.control_channel_id = config.get('control_channel_id')
                    self.control_message_id = config.get('control_message_id')
                    self.bot.logger.log(MODULE_NAME, f"Loaded config: Channel={self.control_channel_id}, Message={self.control_message_id}")
            except Exception as e:
                self.bot.logger.error(MODULE_NAME, "Failed to load config", e)
    
    def save_config(self):
        """Save configuration to file"""
        try:
            config = {
                'control_channel_id': self.control_channel_id,
                'control_message_id': self.control_message_id
            }
            with open(self.config_path, 'w') as f:
                json.dump(config, f, indent=2)
            self.bot.logger.log(MODULE_NAME, "Config saved")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to save config", e)
    
    def init_database(self):
        """Initialize SQLite database with schema"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Projects table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    creator_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    project_type TEXT NOT NULL,
                    status TEXT DEFAULT 'Active',
                    thread_id INTEGER,
                    created_at TEXT NOT NULL
                )
            ''')
            
            # Versions table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS versions (
                    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    version_name TEXT NOT NULL,
                    changelog TEXT,
                    file_url TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES projects (project_id)
                )
            ''')
            
            # Votes table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS votes (
                    vote_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    vote_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, user_id),
                    FOREIGN KEY (project_id) REFERENCES projects (project_id)
                )
            ''')
            
            # Followers table for notifications
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS followers (
                    follow_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, user_id),
                    FOREIGN KEY (project_id) REFERENCES projects (project_id)
                )
            ''')
            
            conn.commit()
            conn.close()
            self.bot.logger.log(MODULE_NAME, "Database initialized")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to initialize database", e)
    
    def get_next_project_id(self) -> str:
        """Generate next project ID (P-001, P-002, etc.)"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM projects")
        count = cursor.fetchone()[0]
        conn.close()
        return f"P-{count + 1:03d}"
    
    def get_stats(self) -> Dict:
        """Get overall statistics"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM projects WHERE status = 'Active'")
        active_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM versions")
        version_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM votes")
        vote_count = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            'active_projects': active_count,
            'total_releases': version_count,
            'total_votes': vote_count
        }
    
    def get_recent_activity(self, limit: int = 5) -> List[str]:
        """Get recent activity from versions and projects"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Get recent versions
        cursor.execute('''
            SELECT v.created_at, p.title, v.version_name, p.creator_id
            FROM versions v
            JOIN projects p ON v.project_id = p.project_id
            ORDER BY v.created_at DESC
            LIMIT ?
        ''', (limit,))
        
        activities = []
        for row in cursor.fetchall():
            created_at, title, version, creator_id = row
            activities.append(f"<@{creator_id}> updated **{title}** to {version}")
        
        conn.close()
        return activities
    
    def get_project(self, project_id: str) -> Optional[Dict]:
        """Get project details"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT project_id, creator_id, title, description, project_type, status, thread_id, created_at
            FROM projects WHERE project_id = ?
        ''', (project_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                'project_id': row[0],
                'creator_id': row[1],
                'title': row[2],
                'description': row[3],
                'project_type': row[4],
                'status': row[5],
                'thread_id': row[6],
                'created_at': row[7]
            }
        return None
    
    def get_current_version(self, project_id: str) -> Optional[Dict]:
        """Get the latest version of a project"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT version_id, version_name, changelog, file_url, created_at
            FROM versions
            WHERE project_id = ?
            ORDER BY created_at DESC
            LIMIT 1
        ''', (project_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                'version_id': row[0],
                'version_name': row[1],
                'changelog': row[2],
                'file_url': row[3],
                'created_at': row[4]
            }
        return None
    
    def get_vote_counts(self, project_id: str) -> Dict[str, int]:
        """Get vote counts for a project"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT vote_type, COUNT(*) 
            FROM votes 
            WHERE project_id = ? 
            GROUP BY vote_type
        ''', (project_id,))
        
        votes = {'fire': 0, 'neutral': 0, 'trash': 0}
        for row in cursor.fetchall():
            votes[row[0]] = row[1]
        
        conn.close()
        return votes
    
    def create_control_center_embed(self) -> discord.Embed:
        """Create the Control Center embed"""
        stats = self.get_stats()
        activities = self.get_recent_activity(3)
        
        embed = discord.Embed(
            title="🎵 Project Hub - Community Edits",
            description="Welcome to the project hub! Click **Create Project** to start a new fan album or edit. Use the buttons below to browse, vote, and manage existing projects.",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="📊 Statistics",
            value=f"**Active Projects:** {stats['active_projects']} | **Total Releases:** {stats['total_releases']} | **Community Votes:** {stats['total_votes']}",
            inline=False
        )
        
        if activities:
            activity_text = "\n".join(f"• {activity}" for activity in activities)
        else:
            activity_text = "No recent activity"
        
        embed.add_field(
            name="📰 Recent Activity",
            value=activity_text,
            inline=False
        )
        
        embed.set_footer(text="Use the buttons below to interact with projects")
        return embed
    
    def create_control_center_view(self) -> discord.ui.View:
        """Create the Control Center view with buttons"""
        view = discord.ui.View(timeout=None)
        
        # Create Project button
        create_button = discord.ui.Button(
            label="Create Project",
            style=discord.ButtonStyle.green,
            emoji="📂",
            custom_id="project_create"
        )
        create_button.callback = self.create_project_callback
        
        # Browse Projects button
        browse_button = discord.ui.Button(
            label="Browse Projects",
            style=discord.ButtonStyle.primary,
            emoji="🔍",
            custom_id="project_browse"
        )
        browse_button.callback = self.browse_projects_callback
        
        # Leaderboard button
        leaderboard_button = discord.ui.Button(
            label="Leaderboard",
            style=discord.ButtonStyle.secondary,
            emoji="🏆",
            custom_id="project_leaderboard"
        )
        leaderboard_button.callback = self.leaderboard_callback
        
        # Help button
        help_button = discord.ui.Button(
            label="Help",
            style=discord.ButtonStyle.secondary,
            emoji="❓",
            custom_id="project_help"
        )
        help_button.callback = self.help_callback
        
        view.add_item(create_button)
        view.add_item(browse_button)
        view.add_item(leaderboard_button)
        view.add_item(help_button)
        
        return view
    
    async def create_project_callback(self, interaction: discord.Interaction):
        """Handle Create Project button click"""
        modal = CreateProjectModal(self)
        await interaction.response.send_modal(modal)
    
    async def browse_projects_callback(self, interaction: discord.Interaction):
        """Handle Browse Projects button click"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT project_id, title, status, project_type
            FROM projects
            ORDER BY created_at DESC
            LIMIT 25
        ''')
        
        projects = cursor.fetchall()
        conn.close()
        
        if not projects:
            await interaction.response.send_message("No projects found!", ephemeral=True)
            return
        
        view = BrowseProjectsView(self, projects)
        
        embed = discord.Embed(
            title="🔍 Browse Projects",
            description="Select a project from the dropdown to view details:",
            color=discord.Color.blue()
        )
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    async def leaderboard_callback(self, interaction: discord.Interaction):
        """Handle Leaderboard button click"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Most popular projects (by fire votes)
        cursor.execute('''
            SELECT p.title, p.project_id, COUNT(v.vote_id) as fire_count
            FROM projects p
            LEFT JOIN votes v ON p.project_id = v.project_id AND v.vote_type = 'fire'
            GROUP BY p.project_id
            ORDER BY fire_count DESC
            LIMIT 5
        ''')
        popular = cursor.fetchall()
        
        # Most prolific creators
        cursor.execute('''
            SELECT creator_id, COUNT(*) as project_count
            FROM projects
            GROUP BY creator_id
            ORDER BY project_count DESC
            LIMIT 5
        ''')
        prolific = cursor.fetchall()
        
        # Most active contributors (by versions)
        cursor.execute('''
            SELECT p.creator_id, COUNT(v.version_id) as version_count
            FROM projects p
            JOIN versions v ON p.project_id = v.project_id
            GROUP BY p.creator_id
            ORDER BY version_count DESC
            LIMIT 5
        ''')
        active = cursor.fetchall()
        
        conn.close()
        
        embed = discord.Embed(
            title="🏆 Project Hub Leaderboard",
            color=discord.Color.gold()
        )
        
        if popular:
            popular_text = "\n".join(f"{i+1}. **{p[0]}** ({p[1]}) - {p[2]} 🔥" for i, p in enumerate(popular))
        else:
            popular_text = "No projects yet"
        embed.add_field(name="🔥 Most Popular Projects", value=popular_text, inline=False)
        
        if prolific:
            prolific_text = "\n".join(f"{i+1}. <@{p[0]}> - {p[1]} projects" for i, p in enumerate(prolific))
        else:
            prolific_text = "No creators yet"
        embed.add_field(name="👑 Most Prolific Creators", value=prolific_text, inline=False)
        
        if active:
            active_text = "\n".join(f"{i+1}. <@{p[0]}> - {p[1]} updates" for i, p in enumerate(active))
        else:
            active_text = "No updates yet"
        embed.add_field(name="⚡ Most Active Contributors", value=active_text, inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    async def help_callback(self, interaction: discord.Interaction):
        """Handle Help button click"""
        embed = discord.Embed(
            title="❓ Project Hub Help",
            description="Here's how to use the Project Hub system:",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="📂 Creating a Project",
            value="Click **Create Project** to start a new project. Fill out the form with your project details, and a dedicated thread will be created for your project.",
            inline=False
        )
        
        embed.add_field(
            name="🔄 Updating Your Project",
            value="In your project's thread, click **Update** to release a new version. You can add changelog notes and a download link.",
            inline=False
        )
        
        embed.add_field(
            name="🗳️ Voting",
            value="Show your appreciation by voting on projects! Use 🔥 for great projects, 😐 for okay ones, or 🗑️ if you think it needs work.",
            inline=False
        )
        
        embed.add_field(
            name="🔔 Following Projects",
            value="Use `/follow <project-id>` to get notified when a project releases a new version.",
            inline=False
        )
        
        embed.add_field(
            name="📊 Project Status",
            value="Projects can be: 🟢 Active, 🟡 On Hold, ✅ Completed, or ⛔ Abandoned",
            inline=False
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    def create_project_master_embed(self, project_id: str) -> discord.Embed:
        """Create the Project Master Embed for a project thread"""
        project = self.get_project(project_id)
        if not project:
            return None
        
        current_version = self.get_current_version(project_id)
        votes = self.get_vote_counts(project_id)
        
        # Status emoji
        status_emoji = {
            'Active': '🟢',
            'On Hold': '🟡',
            'Completed': '✅',
            'Abandoned': '⛔'
        }.get(project['status'], '🟢')
        
        embed = discord.Embed(
            title=f"[{project_id}] {project['title']}",
            description=project['description'],
            color=discord.Color.green()
        )
        
        embed.add_field(name="Status", value=f"{status_emoji} {project['status']}", inline=True)
        embed.add_field(name="Type", value=project['project_type'], inline=True)
        embed.add_field(name="Creator", value=f"<@{project['creator_id']}>", inline=True)
        
        if current_version:
            version_text = f"**{current_version['version_name']}**"
            if current_version['file_url']:
                version_text += f"\n[📥 Download]({current_version['file_url']})"
            embed.add_field(name="Current Version", value=version_text, inline=False)
        
        vote_text = f"🔥 {votes['fire']} | 😐 {votes['neutral']} | 🗑️ {votes['trash']}"
        embed.add_field(name="Community Votes", value=vote_text, inline=False)
        
        embed.set_footer(text=f"Created {project['created_at'][:10]}")
        
        return embed
    
    def create_project_master_view(self, project_id: str, creator_id: int) -> discord.ui.View:
        """Create the Project Master view with buttons"""
        view = discord.ui.View(timeout=None)
        
        # Update button (creator only)
        update_button = discord.ui.Button(
            label="Update",
            style=discord.ButtonStyle.primary,
            emoji="🔄",
            custom_id=f"project_update_{project_id}"
        )
        update_button.callback = lambda i: self.update_project_callback(i, project_id, creator_id)
        
        # Vote button
        vote_button = discord.ui.Button(
            label="Vote",
            style=discord.ButtonStyle.secondary,
            emoji="🗳️",
            custom_id=f"project_vote_{project_id}"
        )
        vote_button.callback = lambda i: self.vote_callback(i, project_id)
        
        # Stats button
        stats_button = discord.ui.Button(
            label="Stats",
            style=discord.ButtonStyle.secondary,
            emoji="📊",
            custom_id=f"project_stats_{project_id}"
        )
        stats_button.callback = lambda i: self.stats_callback(i, project_id)
        
        # Manage button (creator only)
        manage_button = discord.ui.Button(
            label="Manage",
            style=discord.ButtonStyle.danger,
            emoji="⚙️",
            custom_id=f"project_manage_{project_id}"
        )
        manage_button.callback = lambda i: self.manage_callback(i, project_id, creator_id)
        
        view.add_item(update_button)
        view.add_item(vote_button)
        view.add_item(stats_button)
        view.add_item(manage_button)
        
        return view
    
    async def update_project_callback(self, interaction: discord.Interaction, project_id: str, creator_id: int):
        """Handle Update button click"""
        if interaction.user.id != creator_id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only the project creator can update the project!", ephemeral=True)
            return
        
        modal = UpdateProjectModal(self, project_id)
        await interaction.response.send_modal(modal)
    
    async def vote_callback(self, interaction: discord.Interaction, project_id: str):
        """Handle Vote button click"""
        view = VoteView(self, project_id, interaction.user.id)
        await interaction.response.send_message("Cast your vote:", view=view, ephemeral=True)
    
    async def stats_callback(self, interaction: discord.Interaction, project_id: str):
        """Handle Stats button click"""
        project = self.get_project(project_id)
        votes = self.get_vote_counts(project_id)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) FROM versions WHERE project_id = ?', (project_id,))
        version_count = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM followers WHERE project_id = ?', (project_id,))
        follower_count = cursor.fetchone()[0]
        
        conn.close()
        
        total_votes = sum(votes.values())
        
        embed = discord.Embed(
            title=f"📊 Stats for {project['title']}",
            color=discord.Color.blue()
        )
        
        embed.add_field(name="Total Versions", value=str(version_count), inline=True)
        embed.add_field(name="Total Votes", value=str(total_votes), inline=True)
        embed.add_field(name="Followers", value=str(follower_count), inline=True)
        
        embed.add_field(name="🔥 Fire", value=str(votes['fire']), inline=True)
        embed.add_field(name="😐 Neutral", value=str(votes['neutral']), inline=True)
        embed.add_field(name="🗑️ Trash", value=str(votes['trash']), inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    async def manage_callback(self, interaction: discord.Interaction, project_id: str, creator_id: int):
        """Handle Manage button click"""
        if interaction.user.id != creator_id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only the project creator or moderators can manage the project!", ephemeral=True)
            return
        
        view = ManageProjectView(self, project_id)
        await interaction.response.send_message("Manage your project:", view=view, ephemeral=True)
    
    async def refresh_control_center(self):
        """Refresh the Control Center embed"""
        if not self.control_channel_id or not self.control_message_id:
            return
        
        try:
            channel = self.bot.get_channel(self.control_channel_id)
            if not channel:
                return
            
            message = await channel.fetch_message(self.control_message_id)
            embed = self.create_control_center_embed()
            view = self.create_control_center_view()
            
            await message.edit(embed=embed, view=view)
            self.bot.logger.log(MODULE_NAME, "Control Center refreshed")
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to refresh Control Center", e)


class CreateProjectModal(discord.ui.Modal, title="Create New Project"):
    """Modal for creating a new project"""
    
    def __init__(self, hub: ProjectHub):
        super().__init__()
        self.hub = hub
    
    project_title = discord.ui.TextInput(
        label="Project Title",
        placeholder="Enter your project name...",
        max_length=100,
        required=True
    )
    
    description = discord.ui.TextInput(
        label="Short Description",
        placeholder="Describe your project...",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=True
    )
    
    project_type = discord.ui.TextInput(
        label="Project Type",
        placeholder="e.g., Fan Album, Single Edit, Remaster",
        max_length=50,
        required=True
    )
    
    initial_version = discord.ui.TextInput(
        label="Initial Version Name",
        placeholder="e.g., v1.0, First Draft",
        max_length=50,
        required=True
    )
    
    file_url = discord.ui.TextInput(
        label="Download Link (Optional)",
        placeholder="https://...",
        max_length=200,
        required=False
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Generate project ID
            project_id = self.hub.get_next_project_id()
            created_at = datetime.now().isoformat()
            
            # Save to database
            conn = sqlite3.connect(self.hub.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO projects (project_id, creator_id, title, description, project_type, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (project_id, interaction.user.id, str(self.project_title), str(self.description), 
                  str(self.project_type), 'Active', created_at))
            
            # Add initial version
            cursor.execute('''
                INSERT INTO versions (project_id, version_name, changelog, file_url, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (project_id, str(self.initial_version), "Initial release", 
                  str(self.file_url) if self.file_url else None, created_at))
            
            conn.commit()
            conn.close()
            
            # Create thread
            channel = interaction.channel
            embed = self.hub.create_project_master_embed(project_id)
            view = self.hub.create_project_master_view(project_id, interaction.user.id)
            
            # Create thread from the control center message
            if self.hub.control_message_id:
                control_message = await channel.fetch_message(self.hub.control_message_id)
                thread = await control_message.create_thread(
                    name=f"[{project_id}] {self.project_title}",
                    auto_archive_duration=10080  # 7 days
                )
                
                # Update thread_id in database
                conn = sqlite3.connect(self.hub.db_path)
                cursor = conn.cursor()
                cursor.execute('UPDATE projects SET thread_id = ? WHERE project_id = ?', (thread.id, project_id))
                conn.commit()
                conn.close()
                
                # Send project master embed to thread
                await thread.send(embed=embed, view=view)
                
                await interaction.response.send_message(
                    f"✅ Project **{self.project_title}** ({project_id}) created! Check out your thread: {thread.mention}",
                    ephemeral=True
                )
                
                # Refresh control center
                await self.hub.refresh_control_center()
                
                self.hub.bot.logger.log(MODULE_NAME, f"Project {project_id} created by {interaction.user.name}")
            else:
                await interaction.response.send_message(
                    "⚠️ Control Center not set up. Use `/project setup` first!",
                    ephemeral=True
                )
                
        except Exception as e:
            self.hub.bot.logger.error(MODULE_NAME, "Failed to create project", e)
            await interaction.response.send_message("❌ Failed to create project. Please try again.", ephemeral=True)


class UpdateProjectModal(discord.ui.Modal, title="Update Project"):
    """Modal for updating a project with a new version"""
    
    def __init__(self, hub: ProjectHub, project_id: str):
        super().__init__()
        self.hub = hub
        self.project_id = project_id
    
    version_name = discord.ui.TextInput(
        label="Version Name",
        placeholder="e.g., v1.1, Updated Mix",
        max_length=50,
        required=True
    )
    
    changelog = discord.ui.TextInput(
        label="Changelog",
        placeholder="What's new in this version?",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True
    )
    
    file_url = discord.ui.TextInput(
        label="Download Link (Optional)",
        placeholder="https://...",
        max_length=200,
        required=False
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            created_at = datetime.now().isoformat()
            
            # Save version to database
            conn = sqlite3.connect(self.hub.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO versions (project_id, version_name, changelog, file_url, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (self.project_id, str(self.version_name), str(self.changelog),
                  str(self.file_url) if self.file_url else None, created_at))
            
            conn.commit()
            conn.close()
            
            # Update project master embed in thread
            project = self.hub.get_project(self.project_id)
            if project and project['thread_id']:
                thread = interaction.guild.get_thread(project['thread_id'])
                if thread:
                    # Post update notification
                    await thread.send(
                        f"🔄 **Update Released: {self.version_name}**\n"
                        f"By <@{interaction.user.id}>\n\n"
                        f"**Changelog:**\n{self.changelog}"
                    )
                    
                    # Notify followers
                    conn = sqlite3.connect(self.hub.db_path)
                    cursor = conn.cursor()
                    cursor.execute('SELECT user_id FROM followers WHERE project_id = ?', (self.project_id,))
                    followers = cursor.fetchall()
                    conn.close()
                    
                    if followers:
                        mentions = " ".join(f"<@{f[0]}>" for f in followers)
                        await thread.send(f"🔔 {mentions}")
            
            await interaction.response.send_message(
                f"✅ Version **{self.version_name}** released!",
                ephemeral=True
            )
            
            # Refresh control center
            await self.hub.refresh_control_center()
            
            self.hub.bot.logger.log(MODULE_NAME, f"Project {self.project_id} updated to {self.version_name}")
            
        except Exception as e:
            self.hub.bot.logger.error(MODULE_NAME, "Failed to update project", e)
            await interaction.response.send_message("❌ Failed to update project. Please try again.", ephemeral=True)


class VoteView(discord.ui.View):
    """View for voting on a project"""
    
    def __init__(self, hub: ProjectHub, project_id: str, user_id: int):
        super().__init__(timeout=60)
        self.hub = hub
        self.project_id = project_id
        self.user_id = user_id
    
    @discord.ui.button(label="Fire", style=discord.ButtonStyle.success, emoji="🔥")
    async def fire_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cast_vote(interaction, "fire")
    
    @discord.ui.button(label="Neutral", style=discord.ButtonStyle.secondary, emoji="😐")
    async def neutral_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cast_vote(interaction, "neutral")
    
    @discord.ui.button(label="Trash", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def trash_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cast_vote(interaction, "trash")
    
    async def cast_vote(self, interaction: discord.Interaction, vote_type: str):
        try:
            conn = sqlite3.connect(self.hub.db_path)
            cursor = conn.cursor()
            
            # Check if user already voted
            cursor.execute('''
                SELECT vote_type FROM votes WHERE project_id = ? AND user_id = ?
            ''', (self.project_id, self.user_id))
            
            existing_vote = cursor.fetchone()
            
            if existing_vote:
                # Update existing vote
                cursor.execute('''
                    UPDATE votes SET vote_type = ?, created_at = ?
                    WHERE project_id = ? AND user_id = ?
                ''', (vote_type, datetime.now().isoformat(), self.project_id, self.user_id))
                message = f"✅ Vote updated to {vote_type.upper()}!"
            else:
                # Insert new vote
                cursor.execute('''
                    INSERT INTO votes (project_id, user_id, vote_type, created_at)
                    VALUES (?, ?, ?, ?)
                ''', (self.project_id, self.user_id, vote_type, datetime.now().isoformat()))
                message = f"✅ Vote cast: {vote_type.upper()}!"
            
            conn.commit()
            conn.close()
            
            await interaction.response.send_message(message, ephemeral=True)
            
            self.hub.bot.logger.log(MODULE_NAME, f"User {self.user_id} voted {vote_type} on {self.project_id}")
            
        except Exception as e:
            self.hub.bot.logger.error(MODULE_NAME, "Failed to cast vote", e)
            await interaction.response.send_message("❌ Failed to cast vote. Please try again.", ephemeral=True)


class ManageProjectView(discord.ui.View):
    """View for managing a project"""
    
    def __init__(self, hub: ProjectHub, project_id: str):
        super().__init__(timeout=60)
        self.hub = hub
        self.project_id = project_id
    
    @discord.ui.select(
        placeholder="Change project status...",
        options=[
            discord.SelectOption(label="Active", value="Active", emoji="🟢"),
            discord.SelectOption(label="On Hold", value="On Hold", emoji="🟡"),
            discord.SelectOption(label="Completed", value="Completed", emoji="✅"),
            discord.SelectOption(label="Abandoned", value="Abandoned", emoji="⛔")
        ]
    )
    async def status_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        new_status = select.values[0]
        
        try:
            conn = sqlite3.connect(self.hub.db_path)
            cursor = conn.cursor()
            cursor.execute('UPDATE projects SET status = ? WHERE project_id = ?', (new_status, self.project_id))
            conn.commit()
            conn.close()
            
            await interaction.response.send_message(f"✅ Project status updated to **{new_status}**!", ephemeral=True)
            
            self.hub.bot.logger.log(MODULE_NAME, f"Project {self.project_id} status changed to {new_status}")
            
        except Exception as e:
            self.hub.bot.logger.error(MODULE_NAME, "Failed to update project status", e)
            await interaction.response.send_message("❌ Failed to update status. Please try again.", ephemeral=True)


class BrowseProjectsView(discord.ui.View):
    """View for browsing projects"""
    
    def __init__(self, hub: ProjectHub, projects: List):
        super().__init__(timeout=60)
        self.hub = hub
        
        options = []
        for project in projects:
            project_id, title, status, project_type = project
            options.append(
                discord.SelectOption(
                    label=f"{project_id} - {title[:50]}",
                    value=project_id,
                    description=f"{status} | {project_type}"
                )
            )
        
        select = discord.ui.Select(
            placeholder="Select a project to view...",
            options=options
        )
        select.callback = self.select_callback
        self.add_item(select)
    
    async def select_callback(self, interaction: discord.Interaction):
        project_id = interaction.data['values'][0]
        project = self.hub.get_project(project_id)
        
        if not project:
            await interaction.response.send_message("Project not found!", ephemeral=True)
            return
        
        embed = self.hub.create_project_master_embed(project_id)
        
        if project['thread_id']:
            thread = interaction.guild.get_thread(project['thread_id'])
            if thread:
                embed.add_field(name="Thread", value=thread.mention, inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ProjectCommands(commands.Cog):
    """Slash commands for the Project Hub"""
    
    def __init__(self, bot, hub: ProjectHub):
        self.bot = bot
        self.hub = hub
    
    @app_commands.command(name="project", description="Project Hub commands")
    @app_commands.describe(action="Action to perform")
    @app_commands.choices(action=[
        app_commands.Choice(name="setup", value="setup"),
        app_commands.Choice(name="refresh", value="refresh")
    ])
    async def project(self, interaction: discord.Interaction, action: str):
        """Project Hub management commands"""
        if action == "setup":
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("You need 'Manage Server' permission to set up the Project Hub!", ephemeral=True)
                return
            
            embed = self.hub.create_control_center_embed()
            view = self.hub.create_control_center_view()
            
            await interaction.response.send_message(embed=embed, view=view)
            
            # Save the message ID
            message = await interaction.original_response()
            self.hub.control_channel_id = interaction.channel_id
            self.hub.control_message_id = message.id
            self.hub.save_config()
            
            self.bot.logger.log(MODULE_NAME, f"Control Center set up in channel {interaction.channel_id}")
        
        elif action == "refresh":
            await self.hub.refresh_control_center()
            await interaction.response.send_message("✅ Control Center refreshed!", ephemeral=True)
    
    @app_commands.command(name="follow", description="Follow a project to get update notifications")
    @app_commands.describe(project_id="The project ID (e.g., P-001)")
    async def follow(self, interaction: discord.Interaction, project_id: str):
        """Follow a project for notifications"""
        project = self.hub.get_project(project_id)
        
        if not project:
            await interaction.response.send_message("❌ Project not found!", ephemeral=True)
            return
        
        try:
            conn = sqlite3.connect(self.hub.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT OR IGNORE INTO followers (project_id, user_id, created_at)
                VALUES (?, ?, ?)
            ''', (project_id, interaction.user.id, datetime.now().isoformat()))
            
            if cursor.rowcount > 0:
                conn.commit()
                conn.close()
                await interaction.response.send_message(
                    f"✅ You're now following **{project['title']}**! You'll be notified of new updates.",
                    ephemeral=True
                )
                self.bot.logger.log(MODULE_NAME, f"User {interaction.user.id} followed {project_id}")
            else:
                conn.close()
                await interaction.response.send_message(
                    f"ℹ️ You're already following **{project['title']}**!",
                    ephemeral=True
                )
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to follow project", e)
            await interaction.response.send_message("❌ Failed to follow project. Please try again.", ephemeral=True)
    
    @app_commands.command(name="unfollow", description="Unfollow a project")
    @app_commands.describe(project_id="The project ID (e.g., P-001)")
    async def unfollow(self, interaction: discord.Interaction, project_id: str):
        """Unfollow a project"""
        try:
            conn = sqlite3.connect(self.hub.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                DELETE FROM followers WHERE project_id = ? AND user_id = ?
            ''', (project_id, interaction.user.id))
            
            if cursor.rowcount > 0:
                conn.commit()
                conn.close()
                await interaction.response.send_message(f"✅ Unfollowed project {project_id}.", ephemeral=True)
                self.bot.logger.log(MODULE_NAME, f"User {interaction.user.id} unfollowed {project_id}")
            else:
                conn.close()
                await interaction.response.send_message(f"ℹ️ You're not following project {project_id}.", ephemeral=True)
        except Exception as e:
            self.bot.logger.error(MODULE_NAME, "Failed to unfollow project", e)
            await interaction.response.send_message("❌ Failed to unfollow project. Please try again.", ephemeral=True)


def setup(bot):
    """Setup function called by main bot to initialize this module"""
    bot.logger.log(MODULE_NAME, "Setting up Project Hub module")
    
    hub = ProjectHub(bot)
    
    # Add commands
    cog = ProjectCommands(bot, hub)
    asyncio.run_coroutine_threadsafe(bot.add_cog(cog), bot.loop)
    
    bot.logger.log(MODULE_NAME, "Project Hub module setup complete")