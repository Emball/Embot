import json
import argparse
import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from tqdm import tqdm

# --- Configuration matching community.py ---
REACTION_FIRE = "🔥"
REACTION_NEUTRAL = "😐"
REACTION_TRASH = "🗑️"
REACTION_STAR = "⭐"

# Mapping Discord export emoji names to Bot Constants
EMOJI_MAP = {
    "fire": REACTION_FIRE,
    "\U0001f525": REACTION_FIRE,
    "neutral_face": REACTION_NEUTRAL,
    "\U0001f610": REACTION_NEUTRAL,
    "wastebasket": REACTION_TRASH,
    "\U0001f5d1": REACTION_TRASH,
    "star": REACTION_STAR,
    "\u2b50": REACTION_STAR
}

class HistoryImporter:
    def __init__(self):
        # Database Structure matching SubmissionDatabase
        self.data = {
            "submissions": {},
            "user_projects": {},
            "file_hashes": {},
            "link_registry": {},
            "sticky_messages": {},
            "project_versions": {},
            "settings": {
                "sticky_enabled": True,
                "last_spotlight_week": None
            }
        }
        self.stats = {
            "projects": 0,
            "artwork": 0,
            "skipped": 0,
            "votes": 0
        }

    def _get_title_hash(self, title: str) -> str:
        """Replicate bot's title hashing logic"""
        return hashlib.md5(title.lower().strip().encode()).hexdigest()[:8]

    def _generate_pseudo_file_hash(self, url: str) -> str:
        """
        Since we cannot download files to get their actual SHA256 hash,
        we hash the unique Discord URL. This ensures checking for duplicates
        works for exact link matches, though not re-uploads.
        """
        return hashlib.sha256(url.encode()).hexdigest()

    def _extract_links(self, content: str) -> List[str]:
        """Extract URLs from message content"""
        url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
        return re.findall(url_pattern, content)

    def validate_project(self, content: str, attachments: List[Dict]) -> Tuple[bool, Optional[str], Optional[str], List[str]]:
        """Replicate SubmissionValidator.validate_project logic"""
        if not content: 
            return False, None, None, []

        title_match = re.search(r'^#{1,3}\s+(.+)$', content, re.MULTILINE)
        if not title_match:
            return False, None, None, []
        
        title = title_match.group(1).strip()
        
        description_lines = re.findall(r'^\s*-\s+(.+)$', content, re.MULTILINE)
        if not description_lines:
            return False, None, None, []
        
        description = "\n".join(description_lines)
        
        links = self._extract_links(content)
        has_attachment = len(attachments) > 0
        
        # Image-only check: if ONLY images (no links), should go to artwork
        if has_attachment and not links:
            all_images = all(
                any(att.get('fileName', '').lower().endswith(ext) or 
                    att.get('url', '').lower().split('?')[0].endswith(ext)
                    for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp'])
                for att in attachments
            )
            if all_images:
                return False, None, None, []
        
        if not links and not has_attachment:
            return False, None, None, []
        
        media_links = links.copy()
        for att in attachments:
            media_links.append(att.get('url'))
            
        return True, title, description, media_links

    def validate_artwork(self, attachments: List[Dict]) -> Tuple[bool, Optional[str]]:
        """Replicate SubmissionValidator.validate_artwork logic"""
        valid_exts = ['.jpg', '.jpeg', '.png', '.gif', '.webp']
        
        thumbnail = None
        has_image = False
        
        for att in attachments:
            url = att.get('url', '').lower()
            filename = att.get('fileName', '').lower()
            
            # Check URL (before query params), filename, or contentType for image indicators
            # Split URL at '?' to remove query parameters before checking extension
            url_without_params = url.split('?')[0]
            
            is_image = (
                any(url_without_params.endswith(ext) for ext in valid_exts) or
                any(filename.endswith(ext) for ext in valid_exts) or
                'image' in att.get('contentType', '')
            )
            
            if is_image:
                has_image = True
                if not thumbnail:
                    thumbnail = att.get('url')
        
        return has_image, thumbnail

    def process_message(self, msg: Dict, channel_name: str):
        msg_id = str(msg['id'])
        user_id = str(msg['author']['id'])
        content = msg.get('content', '')
        attachments = msg.get('attachments', [])
        timestamp = msg['timestamp'] 

        submission_type = None
        title = None
        description = None
        media_links = []
        thumbnail = None
        
        # 1. Determine Type and Validate
        if "project" in channel_name.lower():
            is_valid, title, description, media_links = self.validate_project(content, attachments)
            if is_valid:
                submission_type = "project"
                # Find thumbnail from attachments
                for att in attachments:
                    url_lower = att.get('url', '').lower()
                    filename_lower = att.get('fileName', '').lower()
                    if any(url_lower.split('?')[0].endswith(ext) or filename_lower.endswith(ext) 
                           for ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif']):
                        thumbnail = att['url']
                        break
            else:
                self.stats['skipped'] += 1
                return

        elif "artwork" in channel_name.lower():
            is_valid, thumb = self.validate_artwork(attachments)
            if is_valid:
                submission_type = "artwork"
                thumbnail = thumb
                media_links = [att['url'] for att in attachments]
            else:
                self.stats['skipped'] += 1
                return
        else:
            return 

        # 2. Versioning Logic
        version = "1.0"
        project_id = ""
        
        if submission_type == "project" and title:
            title_hash = self._get_title_hash(title)
            project_id = f"p-{user_id[-6:]}-{title_hash}"
            version_key = f"{user_id}:{title_hash}"
            
            if version_key in self.data["project_versions"]:
                existing_ids = self.data["project_versions"][version_key]
                latest_version_str = "1.0"
                for eid in existing_ids:
                    if eid in self.data["submissions"]:
                        v = self.data["submissions"][eid]['version']
                        if v > latest_version_str:
                            latest_version_str = v
                
                major = int(float(latest_version_str))
                version = f"{major + 1}.0"
                self.data["project_versions"][version_key].append(msg_id)
            else:
                self.data["project_versions"][version_key] = [msg_id]
        else:
            project_id = f"a-{user_id[-6:]}-{msg_id[-8:]}"

        # 3. Process Votes (Reactions)
        votes = {REACTION_FIRE: 0, REACTION_NEUTRAL: 0, REACTION_TRASH: 0, REACTION_STAR: 0}
        user_votes = {} # user_id -> emoji

        if 'reactions' in msg:
            for react in msg['reactions']:
                emoji_code = react.get('emoji', {}).get('code', '')
                emoji_name = react.get('emoji', {}).get('name', '')
                
                matched_emoji = None
                if emoji_code in EMOJI_MAP: 
                    matched_emoji = EMOJI_MAP[emoji_code]
                elif emoji_name in EMOJI_MAP: 
                    matched_emoji = EMOJI_MAP[emoji_name]
                
                if matched_emoji:
                    # Filter out bot votes
                    valid_voters = []
                    for user in react.get('users', []):
                        # Skip if explicitly marked as bot
                        if user.get('isBot', False):
                            continue
                        valid_voters.append(user)
                    
                    # Recalculate count based only on real users
                    count = len(valid_voters)
                    
                    if count > 0:
                        votes[matched_emoji] += count
                        self.stats['votes'] += count
                        
                        # Populate User Votes only for real users
                        for user in valid_voters:
                            u_id = str(user['id'])
                            user_votes[u_id] = matched_emoji

        # 4. Generate File Hashes (Pseudo)
        file_hashes = []
        for att in attachments:
            pseudo_hash = self._generate_pseudo_file_hash(att['url'])
            file_hashes.append(pseudo_hash)
            self.data["file_hashes"][pseudo_hash] = [user_id, msg_id, project_id]

        # 5. Link Registry
        for link in media_links:
            self.data["link_registry"][link] = [user_id, msg_id, project_id]

        # 6. Build Submission Object
        submission_entry = {
            "project_id": project_id,
            "message_id": msg_id,
            "user_id": user_id,
            "submission_type": submission_type,
            "version": version,
            "title": title,
            "description": description,
            "media_links": media_links,
            "thumbnail": thumbnail,
            "file_hashes": file_hashes,
            "votes": votes,
            "user_votes": user_votes,
            "thread_message_count": 0,
            "thread_message_xp": 0.0,
            "created_at": timestamp,
            "updated_at": timestamp,
            "last_voted_at": timestamp if user_votes else None,
            "last_thread_message_at": None,
            "is_deleted": False,
            "channel_id": str(msg.get('channel', {}).get('id', '0')),
            "linked_submissions": []
        }

        self.data["submissions"][msg_id] = submission_entry
        
        if user_id not in self.data["user_projects"]:
            self.data["user_projects"][user_id] = []
        if project_id not in self.data["user_projects"][user_id]:
            self.data["user_projects"][user_id].append(project_id)

        if submission_type == "project": 
            self.stats['projects'] += 1
        else: 
            self.stats['artwork'] += 1

    def run(self, artwork_path: str, projects_path: str, output_path: str):
        print("⏳ Loading JSON files (this may take a moment for large files)...")
        
        try:
            with open(artwork_path, 'r', encoding='utf-8') as f:
                art_data = json.load(f)
            with open(projects_path, 'r', encoding='utf-8') as f:
                proj_data = json.load(f)
        except Exception as e:
            print(f"❌ Error loading files: {e}")
            sys.exit(1)

        all_messages = []
        
        print("Processing Artwork messages...")
        for msg in art_data.get('messages', []):
            msg['src_channel'] = 'artwork'
            all_messages.append(msg)

        print("Processing Projects messages...")
        for msg in proj_data.get('messages', []):
            msg['src_channel'] = 'projects'
            all_messages.append(msg)

        print(f"Sorting {len(all_messages)} total messages chronologically...")
        try:
            all_messages.sort(key=lambda x: x['timestamp'])
        except KeyError:
            print("❌ Error: Messages missing timestamp field. Check export format.")
            sys.exit(1)

        print("🚀 Starting conversion...")
        for msg in tqdm(all_messages):
            self.process_message(msg, msg['src_channel'])

        print(f"💾 Saving database to {output_path}...")
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

        print("\n✅ Import Complete!")
        print(f"   - Projects Imported: {self.stats['projects']}")
        print(f"   - Artwork Imported: {self.stats['artwork']}")
        print(f"   - Votes Registered: {self.stats['votes']}")
        print(f"   - Skipped (Chat/Invalid): {self.stats['skipped']}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert DiscordChatExporter JSON to Embot Database")
    parser.add_argument("artwork_json", help="Path to the artwork channel export JSON")
    parser.add_argument("projects_json", help="Path to the projects channel export JSON")
    parser.add_argument("-o", "--output", default="community_submissions.json", help="Output database file path")
    
    args = parser.parse_args()
    
    importer = HistoryImporter()
    importer.run(args.artwork_json, args.projects_json, args.output)