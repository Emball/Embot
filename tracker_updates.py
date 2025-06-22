# tracker_updates.py
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from pathlib import Path
import datetime
import discord
from discord.ext import tasks
from config import SCOPE, SHEET_ID, CREDS_FILE

# Default icon URL
ICON_URL = "https://media.discordapp.net/attachments/1009493700738555966/1262676377157505076/OMEGA_QUESTIONN.png"

# Columns to skip (contain scripts/formulas)
SCRIPT_COLUMNS = ['Column_1', 'Total', 'COUNTIFS', 'FILTER', 'REGEXMATCH']

def clean_headers(raw_headers):
    headers = []
    seen = set()
    for i, header in enumerate(raw_headers):
        if not header.strip():
            header = f"Column_{i+1}"
        base_header = header
        counter = 1
        while header in seen:
            header = f"{base_header}_{counter}"
            counter += 1
        seen.add(header)
        headers.append(header)
    return headers

async def setup_google_sheets(bot):
    try:
        if not Path(CREDS_FILE).exists():
            print(f"Credentials file not found at {CREDS_FILE}")
            return False
            
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, SCOPE)
        bot.gc = gspread.authorize(creds)
        if not bot.gc:
            print("Failed to authorize Google Sheets")
            return False
            
        spreadsheet = bot.gc.open_by_key(SHEET_ID)
        if not spreadsheet:
            print(f"Failed to open spreadsheet with ID {SHEET_ID}")
            return False
            
        bot.sheet = spreadsheet.sheet1
        if not bot.sheet:
            print("Failed to access worksheet")
            return False
            
        all_values = bot.sheet.get_all_values()
        bot.cached_values = {}
        
        if all_values:
            headers = clean_headers(all_values[0])
            for idx, row in enumerate(all_values[1:]):
                entry = {}
                for col_idx, value in enumerate(row):
                    header = headers[col_idx] if col_idx < len(headers) else f"Extra_{col_idx+1}"
                    entry[header] = str(value).strip()
                unique_id = f"{entry.get('Name', '')}-{entry.get('Version', '')}".strip('-') or f"row-{idx+2}"
                bot.cached_values[unique_id] = entry
        
        print("Google Sheets connected successfully")
        return True
        
    except Exception as e:
        print(f"Google Sheets error: {str(e)}")
        return False

def get_sheet_diffs(bot, current_data, current_headers):
    diffs = []
    new_cache = {}
    
    if not bot.cached_values:
        for entry in current_data:
            unique_id = f"{entry.get('Name', '')}-{entry.get('Version', '')}".strip('-') or f"row-{len(new_cache)+2}"
            new_cache[unique_id] = entry
            diffs.append({'type': 'add', 'id': unique_id, 'data': entry})
        return diffs, new_cache
    
    old_headers = set(next(iter(bot.cached_values.values())).keys()) if bot.cached_values else set()
    header_changes = {
        'added': list(set(current_headers) - old_headers),
        'removed': list(old_headers - set(current_headers))
    }
    
    for idx, entry in enumerate(current_data):
        unique_id = f"{entry.get('Name', '')}-{entry.get('Version', '')}".strip('-') or f"row-{idx+2}"
        new_cache[unique_id] = entry
        
        if unique_id in bot.cached_values:
            for header in current_headers:
                # Skip script columns
                if any(script in header for script in SCRIPT_COLUMNS):
                    continue
                    
                old_val = bot.cached_values[unique_id].get(header, '')
                new_val = entry.get(header, '')
                if old_val != new_val:
                    diffs.append({
                        'type': 'update',
                        'id': unique_id,
                        'header': header,
                        'old': old_val,
                        'new': new_val,
                        'name': entry.get('Name', 'Unknown Track'),
                        'era': entry.get('Era', 'Unknown Era')
                    })
        else:
            diffs.append({
                'type': 'add', 
                'id': unique_id, 
                'data': entry,
                'name': entry.get('Name', 'New Track'),
                'era': entry.get('Era', 'Unknown Era')
            })
    
    for old_id in bot.cached_values:
        if old_id not in new_cache:
            old_entry = bot.cached_values[old_id]
            diffs.append({
                'type': 'remove', 
                'id': old_id, 
                'data': old_entry,
                'name': old_entry.get('Name', 'Removed Track'),
                'era': old_entry.get('Era', 'Unknown Era')
            })
    
    if header_changes['added'] or header_changes['removed']:
        diffs.append({
            'type': 'headers',
            'changes': header_changes
        })
    
    return diffs, new_cache

@tasks.loop(minutes=10)
async def tracker_update_loop(bot):
    if not hasattr(bot, 'sheet') or not bot.sheet:
        print("Sheet not initialized, skipping update")
        return
    
    try:
        all_values = bot.sheet.get_all_values()
        if not all_values:
            print("No data in sheet, skipping update")
            return
            
        headers = clean_headers(all_values[0])
        current_data = []
        
        for row in all_values[1:]:
            entry = {}
            for col_idx, value in enumerate(row):
                header = headers[col_idx] if col_idx < len(headers) else f"Extra_{col_idx+1}"
                entry[header] = str(value).strip()
            current_data.append(entry)
        
        diffs, new_cache = get_sheet_diffs(bot, current_data, headers)
        if not diffs:
            print("No changes detected")
            return

        # Prevent massive update spam
        update_count = sum(1 for d in diffs if d['type'] == 'update')
        if update_count > 50:  # Threshold for massive updates
            print(f"Suppressing massive update ({update_count} changes)")
            bot.cached_values = new_cache
            return

        bot.cached_values = new_cache
        
        if not bot.tracker_channel:
            print("Tracker channel not found")
            return

        embeds = []
        
        # Process each change
        for change in diffs:
            if change['type'] == 'update':
                # Skip low-value updates
                if change['header'] in ['File Date', 'Last Modified']:
                    continue
                    
                # Create update embed
                embed = discord.Embed(
                    title=f"Updated {change['header']}: {change['name']}",
                    color=discord.Color.from_str("#a56b5d"),  # Brown color from example
                    timestamp=datetime.datetime.utcnow()
                )
                embed.set_author(
                    name="Info",
                    icon_url=ICON_URL
                )
                embed.set_footer(text="Eminem Tracker")
                
                # Add fields
                embed.add_field(
                    name=f"Old {change['header']}",
                    value=change['old'] or "Empty",
                    inline=False
                )
                embed.add_field(
                    name=f"New {change['header']}",
                    value=change['new'],
                    inline=False
                )
                
                embeds.append(embed)
                
            elif change['type'] == 'add':
                # Create add embed
                embed = discord.Embed(
                    title=f"Added: {change['name']}",
                    color=discord.Color.green(),
                    timestamp=datetime.datetime.utcnow()
                )
                embed.set_author(
                    name="Info",
                    icon_url=ICON_URL
                )
                embed.set_footer(text="Eminem Tracker")
                
                # Add fields
                for key, value in change['data'].items():
                    # Skip script columns and empty values
                    if value and not any(script in key for script in SCRIPT_COLUMNS):
                        embed.add_field(
                            name=key,
                            value=value[:100] + "..." if len(value) > 100 else value,
                            inline=True
                        )
                
                embeds.append(embed)
                
            elif change['type'] == 'remove':
                # Create remove embed
                embed = discord.Embed(
                    title=f"Removed: {change['name']}",
                    color=discord.Color.red(),
                    timestamp=datetime.datetime.utcnow()
                )
                embed.set_author(
                    name="Info",
                    icon_url=ICON_URL
                )
                embed.set_footer(text="Eminem Tracker")
                
                embeds.append(embed)
                
            elif change['type'] == 'headers':
                # Create header change embed
                embed = discord.Embed(
                    title="Sheet Structure Changed",
                    color=discord.Color.purple(),
                    timestamp=datetime.datetime.utcnow()
                )
                embed.set_author(
                    name="Info",
                    icon_url=ICON_URL
                )
                embed.set_footer(text="Eminem Tracker")
                
                changes = []
                if change['changes']['added']:
                    changes.append(f"Added columns: {', '.join(change['changes']['added'])}")
                if change['changes']['removed']:
                    changes.append(f"Removed columns: {', '.join(change['changes']['removed'])}")
                    
                embed.description = "\n".join(changes)
                embeds.append(embed)
        
        # Send all embeds in batches of 10
        for i in range(0, len(embeds), 10):
            await bot.tracker_channel.send(embeds=embeds[i:i+10])
        
    except Exception as e:
        print(f"Tracker update failed: {str(e)}")