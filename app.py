"""
MASH Streamlit Application
==========================
Web UI for the MASH text adventure engine.
Version: 58

Architecture: Shared-memory singleton database (classic MUSH style).
All connected users share the same in-memory world state.
Periodic auto-save every 30 minutes, plus manual @dump/@reload for wizards.
"""

import streamlit as st
import os
import hashlib
import time
import random
import threading
import re
from pathlib import Path
from dotenv import load_dotenv
from database import WorldDatabase
from mash_engine import MashEngine
from ai_layer import AIEngine
from streamlit.runtime.scriptrunner import get_script_run_ctx
from streamlit.runtime import get_instance

# ─────────────────────────────────────────────────────────────────
# Safe Synchronization (Multi-Player Support)
# ─────────────────────────────────────────────────────────────────

def check_sync_buffer():
    """Check and drain message buffer for the current player (Interaction/Poll)."""
    if not st.session_state.get("authenticated") or not st.session_state.get("player_ref"):
        return
        
    db = get_db()
    player = db.get_agent(st.session_state.player_ref)
    
    # Check if there are new messages in the buffer
    if player and hasattr(player, 'message_buffer') and player.message_buffer:
        made_changes = False
        for ann in player.message_buffer:
            if not is_near_duplicate(ann, st.session_state.messages):
                st.session_state.messages.append({"role": "assistant", "content": ann})
                made_changes = True
        
        if made_changes:
            # We clear the buffer in the central DB object. 
            # The next save (either auto or manual) will persist this.
            player.message_buffer = [] 
            # Force a rerun of the UI (outside the fragment or via the main loop check)
            st.rerun()

@st.fragment(run_every="3s")
def sync_poll_loop():
    """Safe, legit polling to check for messages from other players."""
    if st.session_state.get("authenticated"):
        check_sync_buffer()

def clean(s):
    """Clean string for comparison (remove icons, bolding, and whitespace)."""
    return s.replace("✨", "").replace("🌐", "").replace("**", "").replace("\n", " ").strip().lower()

def is_near_duplicate(msg, history, limit=5):
    """Check if a message is a near-duplicate of recent history to avoid echoes."""
    if not msg: return True
    clean_msg = clean(msg)
    if not clean_msg: return True
    
    for h in history[-limit:]:
        if h['role'] == 'assistant':
            clean_h = clean(h['content'])
            if not clean_h: continue
            # Check for high overlap or substring
            if clean_msg in clean_h or clean_h in clean_msg:
                return True
    return False

# Load environment variables from .env
load_dotenv()


# ─────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────

# Bump this when mash_engine.py changes to force cache invalidation
ENGINE_VERSION = 61


WORLD_FILE = Path(__file__).parent / "world.json"
START_ROOM = "#0"  # The Arrival is the first room if world is missing
AUTO_SAVE_INTERVAL = 30 * 60  # 30 minutes in seconds


# ─────────────────────────────────────────────────────────────────
# Page Setup
# ─────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="MASH",
    page_icon="🌐",
    layout="wide",
    initial_sidebar_state="expanded"
)

# CSS Hacks for Popover clipping (Global)
st.markdown("""
    <style>
    /* Widen Sidebar to 420px (Optimized for 4x2 grid) */
    [data-testid="stSidebar"] {
        min-width: 420px !important;
        max-width: 420px !important;
    }

    /* Reduce Sidebar "Air" (Top Padding) */
    /* Reduce Sidebar "Air" (Top Padding) */
    [data-testid="stSidebarContent"] {
        padding-top: 1.5rem !important;
        padding-bottom: 150px !important; /* Space for fixed footer */
    }

    /* Ensure content doesn't get hidden behind footer */
    [data-testid="stSidebarContent"] {
        padding-bottom: 160px !important;
    }
    
    /* Force Popovers to have a max-height and use scrolling */
    [data-testid="stPopoverBody"] {
        max-height: 500px !important;
        overflow-y: auto !important;
        border: 1px solid rgba(255,255,255,0.2) !important;
    }
    
    /* Wizard Button Styling (Greyed out for non-wizards) */
    .stButton > button:disabled {
        opacity: 0.5 !important;
        filter: grayscale(100%) !important;
    }

    /* Electric Blue Glow Footer */
    .glow-text {
        color: #00d4ff !important;
        text-shadow: 0 0 8px rgba(0, 212, 255, 0.8);
        font-family: 'Courier New', Courier, monospace;
        font-size: 0.7rem;
        opacity: 1.0;
        margin-top: 5px;
        display: block;
    }

    /* Electric Blue Glow Footer */
    .glow-text {
        color: #00d4ff !important;
        text-shadow: 0 0 8px rgba(0, 212, 255, 0.8);
        font-family: 'Courier New', Courier, monospace;
        font-size: 0.7rem;
        opacity: 1.0;
        margin-top: 10px;
        display: block;
    }
    </style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────
# Shared Database Singleton (Classic MUSH Style)
# ─────────────────────────────────────────────────────────────────

# Bump this when mash_engine.py changes to force cache invalidation
# ENGINE_VERSION moved to top of file


# Default directory for saved snapshots
DEFAULT_SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
DEFAULT_SNAPSHOT_DIR.mkdir(exist_ok=True)

# Default directory for research artifacts
DEFAULT_RESEARCH_DIR = Path(__file__).parent / "research_artifacts"
DEFAULT_RESEARCH_DIR.mkdir(exist_ok=True)

@st.cache_resource
def get_shared_database():
    """
    Load and return the shared world database.
    This is a singleton shared across ALL user sessions.
    """
    db = WorldDatabase()
    if WORLD_FILE.exists():
        db.load(WORLD_FILE)
        print(f"[MASH] Loaded world: {db.meta.get('name')} ({len(db.objects)} objects)")
    else:
        print("[MASH] No world file found, starting with empty world")
        
    # --- MINIMAL STARTUP CHECK ---
    # Ensure room #0 exists if the world is empty
    if not db.objects:
        print("[MASH] Initializing minimal world with Room #0")
        db.create_object(
            'room', 
            'The Arrival', 
            desc="You stand in a shimmering void of potential. A new world begins here."
        )
        
    return db


@st.cache_resource
def get_ai_engine(_version):
    """Load and return the AI generative engine. _version forces refresh."""
    try:
        return AIEngine()
    except Exception as e:
        st.warning(f"AI Engine not initialized: {e}")
        return None

@st.cache_resource
def get_shared_engine(_db, ai_version, _ai, research_path="research_artifacts", snapshot_path="snapshots"):
    """Return the shared engine instance."""
    print(f"[MASH] Creating engine")
    engine = MashEngine(_db, ai_engine=_ai)
    engine.research_path = research_path
    engine.snapshot_path = snapshot_path
    return engine

@st.cache_resource
def get_last_save_time():
    """Track when we last auto-saved (shared across sessions)."""
    return {"timestamp": time.time()}


def get_db():
    """Get the shared database instance."""
    return get_shared_database()


def get_engine():
    """Get the shared engine instance."""
    res_path = st.session_state.get("research_path", "research_artifacts")
    snap_path = st.session_state.get("snapshot_path", "snapshots")
    return get_shared_engine(
        get_db(), 
        ENGINE_VERSION,
        get_ai_engine(ENGINE_VERSION), 
        research_path=res_path,
        snapshot_path=snap_path
    )


# ─────────────────────────────────────────────────────────────────
# Session State Initialization (Per-User)
# ─────────────────────────────────────────────────────────────────

def init_session_state():
    """Initialize per-user session state."""
    "Research path init"
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    
    if "player_ref" not in st.session_state:
        st.session_state.player_ref = None

    # Reset player_ref if version changed or explicit reset needed
    # (Actually we can't easily detect version change here without persistent storage, 
    # but we can rely on standard session logic)
    
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "last_snapshot" not in st.session_state:
        st.session_state.last_snapshot = None
    if "last_visual_prompt" not in st.session_state:
        st.session_state.last_visual_prompt = ""
    if "snapshot_path" not in st.session_state:
        st.session_state.snapshot_path = str(DEFAULT_SNAPSHOT_DIR)
    if "gallery_index" not in st.session_state:
        st.session_state.gallery_index = 0
    if "research_path" not in st.session_state:
        st.session_state.research_path = str(DEFAULT_RESEARCH_DIR)
    if "research_index" not in st.session_state:
        st.session_state.research_index = 0
    if "pending_chain" not in st.session_state:
        st.session_state.pending_chain = None
    if "last_view" not in st.session_state:
        st.session_state.last_view = None



init_session_state()


# ─────────────────────────────────────────────────────────────────
# Utility Functions
# ─────────────────────────────────────────────────────────────────

def parse_input_stream(text):
    """
    Parse input into commands. 
    Supports {} as a 'scripting block' container for multiline/multi-command pastes.
    Standard [] are preserved for MASHcode function evaluation.
    """
    if not text:
        return []

    cmds = []
    current = []
    in_script_block = False
    
    for char in text:
        if char == '{' and not in_script_block:
            in_script_block = True
            continue
        elif char == '}' and in_script_block:
            in_script_block = False
            # Flush on block close
            if current:
                block_content = "".join(current).strip()
                # Split by newline or semicolon inside the block
                for sub_cmd in re.split(r'[\n;]', block_content):
                    cleaned = sub_cmd.strip()
                    if cleaned and not cleaned.startswith('#'):
                        cmds.append(cleaned)
                current = []
            continue
        
        if not in_script_block and char == '\n':
            # Horizontal flush for single-line commands
            if current:
                cleaned = "".join(current).strip()
                if cleaned and not cleaned.startswith('#'):
                    cmds.append(cleaned)
                current = []
        else:
            current.append(char)
    
    # Final flush
    if current:
        cleaned = "".join(current).strip()
        if cleaned and not cleaned.startswith('#'):
            cmds.append(cleaned)
            
    return cmds

def hash_password(password: str) -> str:
    """Simple password hashing. NOT secure for production!"""
    return hashlib.sha256(password.encode()).hexdigest()[:16]


def find_player_by_name(name: str):
    """Find a player agent by name (case-insensitive)."""
    db = get_db()
    name_lower = name.lower().strip()
    for obj in db.objects.values():
        if obj.type == 'agent' and not obj.autonomous:
            if obj.name.lower() == name_lower:
                return obj
    return None


def count_existing_players() -> int:
    """Count how many player characters exist (non-autonomous agents)."""
    db = get_db()
    count = 0
    for obj in db.objects.values():
        if obj.type == 'agent' and not obj.autonomous:
            count += 1
    return count


def save_world(announce: bool = False) -> str:
    """Save the world state to disk."""
    db = get_db()
    db.save(WORLD_FILE)
    get_last_save_time()["timestamp"] = time.time()
    msg = f"GAME: Database saved. ({len(db.objects)} objects)"
    if announce:
        print(f"[MASH] {msg}")
    return msg


def reload_world() -> str:
    """Reload the world from disk (discards unsaved changes!)."""
    db = get_db()
    if WORLD_FILE.exists():
        db.load(WORLD_FILE)
        return f"GAME: Database reloaded from disk. ({len(db.objects)} objects)"
    else:
        return "GAME: No world file found!"


def check_auto_save():
    """Check if it's time for an auto-save and idle-check."""
    last_save = get_last_save_time()
    elapsed = time.time() - last_save["timestamp"]
    if elapsed >= AUTO_SAVE_INTERVAL:
        # Also perform a quick idle check for autonomous agents
        try:
            get_engine().check_idle_agents(timeout_seconds=300)
        except:
            pass
            
        save_world(announce=True)
        return True
    return False




def execute_sidebar_cmd(cmd_text):
    """Execute a command from sidebar UI and update state (Reload 6)."""
    engine = get_engine()
    player_ref = st.session_state.player_ref
    if not player_ref:
        return
        
    # Support multi-line commands (split and sequence)
    commands = parse_input_stream(cmd_text)
    
    for cmd in commands:
        result = engine.process_command(player_ref, cmd)
        
        # Determine if this is a silent/system command
        is_system = (result.context.get('category') == 'System') if result.context else False
        
        if is_system:
            if result.message:
                st.toast(result.message)
        else:
            # Update Chat History only for non-system actions
            if not cmd.startswith(('&', '@memo', '@status', '@upsum')):
                st.session_state.messages.append({"role": "user", "content": cmd})
            if result.message:
                st.session_state.messages.append({"role": "assistant", "content": result.message})
    
    # Process Message Buffer (for async events like auto-gaze)
    db = get_db()
    player = db.get_agent(player_ref)
    if player and getattr(player, 'message_buffer', []):
        for msg in player.message_buffer:
            st.session_state.messages.append({"role": "assistant", "content": msg})
        player.message_buffer = []  # Clear buffer
    
    # Sync Reactions
    if result.success:
         # Note: player already retrieved above
         if player:
             # Set primary trigger (On-screen)
             st.session_state.pending_chain = {
                'room': player.location,
                'actor': player_ref,
                'action': cmd_text,
                'is_offscreen': False
             }
             
             # Handle off-screen trigger (Departure)
             old_loc = result.context.get('from_room', {}).get('dbref') if result.context else None
             if old_loc and old_loc != player.location:
                 # Trigger departure reactions in the old room
                 off_res = engine.trigger_room_reactions(old_loc, player_ref, f"PRESENCE_DEPARTURE {player.name}")
                 for r in off_res:
                     if r['narrative']:
                         st.session_state.messages.append({"role": "assistant", "content": f"✨ {r['narrative']}"})
    st.rerun()


def is_wizard(player_ref: str) -> bool:
    """Check if a player has wizard privileges."""
    db = get_db()
    player = db.get_agent(player_ref)
    return player and getattr(player, 'wizard', False)


# ─────────────────────────────────────────────────────────────────
# Wizard Commands
# ─────────────────────────────────────────────────────────────────

def handle_wizard_command(player_ref: str, cmd: str) -> str | None:
    """
    Handle wizard-only @ commands.
    Returns response message if handled, None if not a wizard command.
    """
    cmd_lower = cmd.lower().strip()
    
    if cmd_lower == "@dump":
        if not is_wizard(player_ref):
            return "Permission denied. Wizard powers required."
        return save_world(announce=True)
    
    if cmd_lower == "@reload":
        if not is_wizard(player_ref):
            return "Permission denied. Wizard powers required."
        return reload_world()
    
    if cmd_lower in ["@purge_buffers", "@purge_buffer"]:
        if not is_wizard(player_ref):
            return "Permission denied. Wizard powers required."
        engine = get_engine()
        result = engine._cmd_purge_buffers(player_ref, "")
        save_world(announce=False) # Manual save after purge
        return result.message
    
    return None  # Not a wizard command

def process_robot_ticks(player_ref: str):
    """Trigger AI processing for all robot agents."""
    db = get_db()
    ai = get_ai_engine(ENGINE_VERSION)
    engine = get_engine()
    
    if not ai:
        return "AI Engine not available."
        
    robots = [obj for obj in db.objects.values() if obj.type == 'agent' and getattr(obj, 'robot', False)]
    if not robots:
        return "No robot agents found in the database."
        
    responses = []
    for robot in robots:
        # Get context (localized to their room)
        ctx = engine.get_ai_context(robot.dbref, robot.dbref, 'tick')
        # Generate action
        ai_output = ai.get_robot_tick(ctx)
        # Add the 'narrative' part to the history/stream if it has any
        clean_narrative = re.sub(r'\[.*?\]', '', ai_output).strip()
        if clean_narrative:
            # We treat this as an 'emit' or 'say' from the robot
            # For now, let's just log it in history as if they said it
            engine._add_to_history(robot.dbref, "AI_TICK", f"{robot.name}: {clean_narrative}")
            responses.append(f"**{robot.name}**: {clean_narrative}")
        
        # Capture and execute intents
        intents = engine.capture_robot_intent(robot.dbref, ai_output)
        for res in intents:
            if res.message:
                responses.append(f"  ↳ {res.message}")
                
    return "\n".join(responses) if responses else "Robots are idling."


# ─────────────────────────────────────────────────────────────────
# Login / Connect Screen
# ─────────────────────────────────────────────────────────────────

def show_login_screen():
    """Show the login/connect screen."""
    st.title("🌐 MASH")
    st.subheader("Multi-Agent Semantic Hallucination")
    
    st.divider()
    
    # Tabs for Connect vs Create
    tab_connect, tab_create = st.tabs(["🔑 Connect", "✨ Create Character"])
    
    # ─────────────────────────────────────────────────────────────
    # Connect Tab (Login)
    # ─────────────────────────────────────────────────────────────
    with tab_connect:
        st.markdown("### Connect to Existing Character")
        
        with st.form("login_form"):
            login_name = st.text_input("Character Name", placeholder="Enter your character name...")
            login_password = st.text_input("Password", type="password", placeholder="Enter password...")
            
            connect_btn = st.form_submit_button("🔑 Connect", width="stretch")
            
            if connect_btn:
                if not login_name.strip():
                    st.error("Please enter your character name!")
                elif not login_password:
                    st.error("Please enter your password!")
                else:
                    # Look up player
                    player = find_player_by_name(login_name)
                    
                    if not player:
                        st.error(f"Character '{login_name}' not found. Create a new character?")
                    else:
                        # Check password
                        stored_hash = getattr(player, 'password_hash', None)
                        if not stored_hash:
                            st.error("This character has no password set. Contact admin.")
                        elif hash_password(login_password) != stored_hash:
                            st.error("Incorrect password!")
                        else:
                            st.session_state.authenticated = True
                            st.session_state.player_ref = player.dbref
                            
                            # Initialize chat
                            engine = get_engine()
                            result = engine.process_command(player.dbref, "look")
                            
                            wiz_msg = " You have **Wizard** powers." if getattr(player, 'wizard', False) else ""
                            st.session_state.messages = [
                                {"role": "system", "content": f"Welcome back, **{player.name}**!{wiz_msg}"},
                                {"role": "assistant", "content": result.message}
                            ]
                            st.rerun()
    
    # ─────────────────────────────────────────────────────────────
    # Create Character Tab
    # ─────────────────────────────────────────────────────────────
    with tab_create:
        st.markdown("### Create New Character")
        
        with st.form("create_form"):
            new_name = st.text_input(
                "Character Name",
                placeholder="Choose a unique name...",
                max_chars=50
            )
            
            new_password = st.text_input(
                "Password",
                type="password",
                placeholder="Choose a password..."
            )
            
            confirm_password = st.text_input(
                "Confirm Password",
                type="password",
                placeholder="Confirm your password..."
            )
            
            new_desc = st.text_area(
                "Description",
                placeholder="Describe your character's appearance and personality...",
                max_chars=500,
                height=100
            )
            
            create_btn = st.form_submit_button("✨ Create & Enter", width="stretch")
            
            if create_btn:
                # Validation
                if not new_name.strip():
                    st.error("Please enter a character name!")
                elif len(new_name.strip()) < 2:
                    st.error("Character name must be at least 2 characters!")
                elif not new_password:
                    st.error("Please choose a password!")
                elif len(new_password) < 4:
                    st.error("Password must be at least 4 characters!")
                elif new_password != confirm_password:
                    st.error("Passwords don't match!")
                elif find_player_by_name(new_name):
                    st.error(f"Character '{new_name}' already exists! Choose another name.")
                else:
                    # Check if this is the first player (gets wizard flag)
                    is_first_player = count_existing_players() == 0
                    
                    # Create new player
                    db = get_db()
                    player = db.create_object(
                        'agent',
                        new_name.strip(),
                        desc=new_desc.strip() if new_desc.strip() else "A mysterious traveler.",
                        autonomous=False,
                        location=START_ROOM
                    )
                    
                    # Store password hash
                    player.password_hash = hash_password(new_password)
                    
                    # Players own themselves
                    player.owner = player.dbref
                    
                    # First player becomes wizard!
                    if is_first_player:
                        player.wizard = True
                    else:
                        # Non-wizards get starting tokens
                        player.tokens = 100
                    
                    # Save world with new player
                    save_world()
                    
                    # Authenticate
                    st.session_state.authenticated = True
                    st.session_state.player_ref = player.dbref
                    
                    # Initialize chat with appropriate welcome
                    engine = get_engine()
                    
                    # Presence Announcement: Connect
                    engine._announce_arrival(player.dbref, START_ROOM, "has connected.")
                    
                    result = engine.process_command(player.dbref, "look")
                    
                    if is_first_player:
                        welcome_msg = f"🧙 Welcome, **{player.name}**! You are the first to arrive, and have been granted **Wizard** powers."
                    else:
                        welcome_msg = f"Welcome to **MASH**, {player.name}! Your adventure begins... (You have **100 Tokens** to start.)"
                    
                    st.session_state.messages = [
                        {"role": "system", "content": welcome_msg},
                        {"role": "assistant", "content": result.message}
                    ]
                    st.rerun()


# ─────────────────────────────────────────────────────────────────
# Check if login is needed
# ─────────────────────────────────────────────────────────────────

if not st.session_state.authenticated:
    show_login_screen()
    st.stop()

# --- INSTANT SYNC HOOKS ---
# (Background polling handled at the end of script for stability)
check_sync_buffer() 


# ─────────────────────────────────────────────────────────────────
# Auto-save check (runs on each page load)
# ─────────────────────────────────────────────────────────────────

check_auto_save()


# ─────────────────────────────────────────────────────────────────
# Sidebar: Room Info
# ─────────────────────────────────────────────────────────────────





def render_outfit_manager(player_ref: str, mode: str = "all"):
    """
    Render the Outfit Manager content.
    mode: 'all', 'self', or 'others'
    """
    db = get_db()
    player = db.get_agent(player_ref)
    if not player: return

    # 1. Target Selection
    all_targets = []
    if mode in ["all", "self"]:
        all_targets.append(player)
    if mode in ["all", "others"]:
        for obj in db.objects.values():
            if obj.type == 'agent' and obj.dbref != player_ref:
                if getattr(obj, 'owner', '') == player_ref:
                    all_targets.append(obj)
    
    if not all_targets:
        st.info("No targets available.")
        return

    # Select Target
    target = all_targets[0]
    if len(all_targets) > 1:
        target_names = [f"{t.name} ({t.dbref})" for t in all_targets]
        key_base = f"outfit_tgt_{mode}"
        selected_name = st.selectbox("Select Target", target_names, key=f"sel_{key_base}")
        try:
             target_idx = target_names.index(selected_name)
             target = all_targets[target_idx]
        except ValueError:
             target = all_targets[0]
    elif mode == 'others':
         # Only 1 agent, but still show label
         st.markdown(f"**Target:** {target.name}")

    st.divider()
    
    # 2. Slot Selection (Submenu)
    # Use columns or pills if available? Stick to selectbox / radio for reliability.
    # Radio is nice for 10 items? Might take space. Selectbox is compact.
    # Let's try a horizontal radio if it fits, or just selectbox.
    # "Select Slot"
    
    # Create labels for slots (show emptiness?)
    slot_labels = {}
    for i in range(1, 11):
        key = f"outfit_{i}"
        has_desc = bool(target.attrs.get(key))
        # Icon: 👕 if filled, ⚪ if empty
        ico = "👕" if has_desc else "⚪"
        slot_labels[i] = f"{ico} Slot {i}"
        
    slot_id = st.selectbox(
        "Select Slot", 
        options=range(1, 11), 
        format_func=lambda i: slot_labels[i],
        key=f"slot_sel_{mode}_{target.dbref}"
    )
    
    # 3. Slot Interface
    slot_key = f"outfit_{slot_id}"
    current_desc = target.attrs.get(slot_key, "")
    
    # Preview Frame
    if current_desc:
        st.success(f"**Current Look:**\n\n{current_desc}")
        if st.button(f"✨ Wear This Outfit", key=f"btn_wear_{target.dbref}_{slot_id}", use_container_width=True):
             cmd = f"@wear {slot_id}"
             if target != player:
                 cmd = f"@wear {target.name} {slot_id}"
             execute_sidebar_cmd(cmd)
    else:
        st.warning("Empty Slot")
    
    st.markdown("---")
    
    # Edit Form
    with st.expander("✏️ Edit Description", expanded=not current_desc):
        # Unique key including slot
        form_key = f"edit_form_{target.dbref}_{slot_id}"
        with st.form(key=form_key):
             new_desc = st.text_area("Description", value=current_desc, height=120)
             if st.form_submit_button("💾 Save to Slot"):
                 safe_desc = new_desc.replace("\n", " ")
                 cmd = f"@outfit define {slot_id}={safe_desc}"
                 if target != player:
                      cmd = f"@outfit define {target.name} {slot_id}={safe_desc}"
                 execute_sidebar_cmd(cmd)




def render_construction_menu(player_ref: str):
    """
    Render the World-Building / Construction sidebar section.
    Provides easy access to @dig, @create, @link, and @describe.
    """
    db = get_db()
    player = db.get_agent(player_ref)
    if not player:
        return

    # Row 1: Dig | Create
    c1_r1, c1_r2 = st.columns(2)
    with c1_r1:
        # 1. DIG ROOM
        with st.popover("🏗️ Dig Room", use_container_width=True):
            st.markdown("**Create a New Room (10 Tokens)**")
            room_name = st.text_input("New Room Name", placeholder="The Crystal Grotto", key="dig_name")
            if st.button("Dig", use_container_width=True) and room_name:
                execute_sidebar_cmd(f"@dig {room_name}")
                
    with c1_r2:
        # 2. CREATE (Object/Agent)
        with st.popover("📦 Create", use_container_width=True):
            tab_obj, tab_npc = st.tabs(["Object (1)", "Agent (5)"])
            with tab_obj:
                obj_name = st.text_input("Object Name", placeholder="a floating lamp", key="create_obj_name")
                if st.button("Create Object", use_container_width=True) and obj_name:
                    execute_sidebar_cmd(f"@create {obj_name}")
            with tab_npc:
                npc_name = st.text_input("Agent Name", placeholder="Gus the Golem", key="create_npc_name")
                is_rob = st.checkbox("Enable AI (Robot Mode)", value=False, key="create_npc_robot")
                if st.button("Spawn Agent", use_container_width=True) and npc_name:
                    cmd = f"@agent {npc_name}"
                    if is_rob:
                        # Chain command: create then set robot
                        cmd += f"\n@robot {npc_name}=yes"
                    execute_sidebar_cmd(cmd)

    # Row 2: Link | Describe
    c2_r1, c2_r2 = st.columns(2)
    with c2_r1:
        # 3. LINK (Intuitive Exits)
        with st.popover("🔗 Link Exit", help="Connect current room to another.", use_container_width=True):
            st.markdown("**Create an Exit (1 Token)**")
            exit_name = st.text_input("Exit Name", placeholder="north", key="link_exit_name")
            
            # Room Selection
            owned_rooms = sorted([r for r in db.objects.values() if r.type == 'room' and r.owner == player_ref], key=lambda x: x.name)
            if not owned_rooms:
                st.warning("No owned rooms found to link to.")
            else:
                dest_options = [f"{r.name} ({r.dbref})" for r in owned_rooms]
                selected_dest = st.selectbox("Destination Room", options=dest_options, key="link_dest_sel")
                dest_ref = selected_dest.split('(')[-1].strip(')')

                # Return Exit Logic
                add_return = st.checkbox("Add Return Exit", value=True, key="link_add_return")
                
                # Auto-infer opposite if common direction
                opposites = {
                    "north": "south", "south": "north", "east": "west", "west": "east",
                    "n": "s", "s": "n", "e": "w", "w": "e",
                    "up": "down", "down": "up", "u": "d", "d": "u",
                    "in": "out", "out": "in"
                }
                default_back = opposites.get(exit_name.lower().strip(), "")
                back_name = st.text_input("Return Exit Name", value=default_back, placeholder="south", key="link_back_name") if add_return else ""

                if st.button("Create Link", use_container_width=True) and exit_name and dest_ref:
                    # Primary Link
                    cmd = f"@link {exit_name}={dest_ref}"
                    # Return Link if requested
                    if add_return and back_name:
                        orig_ref = player.location
                        cmd += f"\n@tel {dest_ref}\n@link {back_name}={orig_ref}\n@tel {orig_ref}"
                    execute_sidebar_cmd(cmd)

    with c2_r2:
        # 4. DESCRIBE
        with st.popover("✏️ Describe", use_container_width=True):
            # Target Selection
            location = db.get(player.location)
            room_contents = db.get_room_contents(location.dbref) if location else []
            inventory = [db.get(i) for i in player.inventory if db.get(i)]
            
            if location:
                label = "Here (Room)" if location.type == 'room' else f"Inside: {location.name}"
                desc_targets = [(label, location.dbref)]
            else:
                desc_targets = [("Nowhere", "")]
            for item in room_contents:
                 if player_ref == item.owner or player.wizard:
                     desc_targets.append((f"{item.name} ({item.dbref})", item.dbref))
            for item in inventory:
                 desc_targets.append((f"🎒 {item.name} ({item.dbref})", item.dbref))
                 
            target_labels = [t[0] for t in desc_targets]
            selected_label = st.selectbox("Describe Target", target_labels, key="desc_target_sel")
            target_ref = next(t[1] for t in desc_targets if t[0] == selected_label)
            
            target_obj = db.get(target_ref)
            current_desc = target_obj.desc if target_obj else ""
            
            new_desc = st.text_area("New Description", value=current_desc, height=150, key="desc_val")
            if st.button("Set Description", use_container_width=True):
                 safe_desc = new_desc.replace("\n", " ").strip()
                 execute_sidebar_cmd(f"@describe {target_ref}={safe_desc}")

def render_sidebar():
    """Render the sidebar with current room info."""

    db = get_db()
    player_ref = st.session_state.player_ref
    player = db.get_agent(player_ref)
    
    if not player:
        st.sidebar.error("Player not found!")
        return
    
    # Generic location retrieval (Supports Rooms AND Vehicles)
    location = db.get(player.location)
    if not location:
        st.sidebar.error("You are nowhere!")
        return
        
    is_room = (location.type == 'room')
    
    # --- VERSION INFO (TOP) ---
    st.sidebar.markdown(f"<div class='glow-text' style='text-align: center; margin-bottom: 10px;'>Lexideck MASH v{ENGINE_VERSION} | World Ready</div>", unsafe_allow_html=True)
    
    # --- NAVIGATION ---
    st.sidebar.radio(
        "Navigation", ["Chat", "Snapshot", "Research"],
        format_func=lambda x: {"Chat": "💬 Chat", "Snapshot": "🖼️ Snapshots", "Research": "📚 Research"}.get(x, x),
        label_visibility="collapsed", key="main_view_mode", horizontal=True
    )

    # --- Header ---
    # --- Header ---
    wiz_badge = " 🧙" if getattr(player, 'wizard', False) else ""
    
    # Layout: Name | Disconnect
    h_col1, h_col2 = st.sidebar.columns([2, 1])
    with h_col1:
        st.markdown(f"### 👤 {player.name}{wiz_badge}")
    with h_col2:
        # Align button to be somewhat vertically centered with the header text
        # Using a little spacer or just relying on natural alignment
        st.write("") # Spacer
        if st.button("Goodbye! 🚪", help="Disconnect / Logout", use_container_width=True):
             st.session_state.authenticated = False
             st.rerun()
    
    tokens = getattr(player, 'tokens', 0)
    token_disp = "∞" if getattr(player, 'wizard', False) else f"{tokens}"
    
    # Vehicle Icons
    v_type = getattr(location, 'vehicle_type', '').lower()
    icon_map = {
        'bike': '🚲',
        'boat': '🛥️',
        'car': '🚗',
        'helicopter': '🚁',
        'plane': '✈️',
        'rocket': '🚀'
    }
    
    loc_icon = icon_map.get(v_type, "📍") if is_room else icon_map.get(v_type, "🚌")
    loc_name = location.name
    if not is_room:
        loc_name = f"Inside: {location.name}"
        
    st.sidebar.caption(f"Tokens: {token_disp} | {loc_icon} {loc_name}")
    
    # Check for Shadow Commands (Vehicle Controls)
    # Commands defined as attributes starting with $ (e.g. &CMD #123=$steer *:say Steering...)
    shadow_cmds = []
    if not is_room:
        for k, v in location.attrs.items():
            # Check value (v) for $command:action pattern, NOT the key (k)
            if v.startswith('$') and ':' in v:
                # Parse command syntax: $steer *:say ...
                parts = v.split(':', 1)
                left_side = parts[0].replace('$', '').strip()
                right_side = parts[1].strip() if len(parts) > 1 else ""
                
                # Label logic: Use full text, don't truncate at space!
                if '*' in left_side:
                    display_label = left_side.split('*')[0].strip()
                else:
                    display_label = left_side
                    
                # Detect "go" action for better icons
                is_go_cmd = right_side.lower().startswith('go ')
                
                shadow_cmds.append((display_label, left_side, right_side, k, is_go_cmd))
    
    # (Shadow Commands logic moved below into Travel section)
        
    
    
    # --- ACTION DASHBOARD ---
    st.sidebar.markdown("### ⚡ Action Dashboard")
    
    # Get Context Data
    exits = db.get_room_exits(location.dbref) if is_room else []
    room_contents = db.get_room_contents(location.dbref)
    room_objects = [o for o in room_contents if o.type == 'object']
    others = [a for a in room_contents if a.dbref != player.dbref and a.type == 'agent']
    inventory = [db.get(i) for i in player.inventory if db.get(i)]
    is_wiz = getattr(player, 'wizard', False)

    # 1. TRAVEL SECTION
    with st.sidebar.expander("🚶 Travel", expanded=True):
        # Grid Layout for Destinations & Actions
        # We want a 2-column grid.
        
        buttons = []
        
        # [NEW] Go Menu (Consolidated Exits)
        buttons.append({"label": "🏃 Go", "is_go_menu": True})
        
        # Vehicles (Board/Enter)
        enterables = [obj for obj in room_contents if getattr(obj, 'enter_ok', False)]
        if enterables:
            for veh in enterables:
                ico = "🛥️" if "boat" in getattr(veh, 'vehicle_type', '').lower() else "🚪"
                buttons.append({
                    "label": f"{ico} {veh.name}",
                    "key": f"ent_{veh.dbref}",
                    "cmd": f"enter {veh.name}"
                })
        
        # Local Actions
        buttons.append({"label": "🏠 Home", "key": "btn_home", "cmd": "home"})
        if not is_room:
            buttons.append({"label": "🚪 Exit", "key": "btn_exit", "cmd": "exit"})

        # Wizard Teleport (Integrated)
        if is_wiz:
            buttons.append({"label": "🔮 Teleport", "is_teleport": True})

        # [NEW] Vehicle Controls Integration
        if shadow_cmds:
            buttons.append({"label": f"{loc_icon} Controls", "is_v_controls": True})

        # Render Loop
        cols = st.columns(2)
        for i, btn in enumerate(buttons):
            with cols[i % 2]:
                if btn.get('is_go_menu'):
                    with st.popover(btn['label'], use_container_width=True):
                        if exits:
                            go_cols = st.columns(2)
                            for i, ex in enumerate(exits):
                                dest = db.get_room(ex.destination)
                                dname = dest.name if dest else "???"
                                with go_cols[i % 2]:
                                    if st.button(f"{ex.name} ({dname})", key=f"go_{ex.dbref}", use_container_width=True):
                                        execute_sidebar_cmd(f"go {ex.name}")
                        else:
                            st.caption("No exits.")
                elif btn.get('is_teleport'):
                    with st.popover(btn['label'], help="Wizard Teleport", use_container_width=True):
                        owned = sorted([o for o in db.objects.values() if o.type == 'room' and o.owner == player.dbref], key=lambda x: x.name)
                        if owned:
                            tp_cols = st.columns(2)
                            for i, r in enumerate(owned):
                                with tp_cols[i % 2]:
                                    if st.button(f"🌀 {r.name}", key=f"tp_{r.dbref}", use_container_width=True):
                                        execute_sidebar_cmd(f"@tel me={r.dbref}")
                        else: st.caption("No owned rooms.")
                elif btn.get('is_v_controls'):
                    with st.popover(btn['label'], help=f"Vehicle Controls for {location.name}", use_container_width=True):
                        st.markdown("### Vehicle Controls")
                        simple_cmds = [item for item in shadow_cmds if '*' not in item[1]]
                        complex_cmds = [item for item in shadow_cmds if '*' in item[1]]
                        
                        if simple_cmds:
                            sc_cols = st.columns(2)
                            for i, (label, full_pat, action, attr_key, is_go) in enumerate(simple_cmds):
                                icon = "⚓" if is_go else "⚡"
                                with sc_cols[i % 2]:
                                    if st.button(f"{icon} {label}", key=f"vcl_btn_{attr_key}", use_container_width=True):
                                        execute_sidebar_cmd(full_pat)
                        if simple_cmds and complex_cmds: st.divider()
                        for label, full_pat, action, attr_key, is_go in complex_cmds:
                            val = st.text_input(f"{label} ...", key=f"vcl_in_{attr_key}", placeholder="args...")
                            if st.button(f"Execute", key=f"vcl_exec_{attr_key}", use_container_width=True):
                                execute_sidebar_cmd(f"{full_pat.replace('*', val or '')}")
                else:
                    if st.button(btn['label'], key=btn['key'], use_container_width=True):
                        execute_sidebar_cmd(btn['cmd'])

    # 2. INTERACTION SECTION
    with st.sidebar.expander("🎭 Interaction", expanded=True):
        # Target logic for senses
        # Format: (label, command_target, unique_suffix)
        target_label = "Room" if is_room else "Interior"
        sense_targets = [(target_label, "", "room")]
        if not is_room: sense_targets.append(("Outside", "out", "out"))
        
        # Use dbref for uniqueness
        for a in others: sense_targets.append((f"👤 {a.name}", a.name, a.dbref))
        for o in room_objects: sense_targets.append((f"📦 {o.name}", o.name, o.dbref))

        # Row 1: Look | Hear
        i_r1_c1, i_r1_c2 = st.columns(2)
        with i_r1_c1:
            with st.popover("👁️ Look", use_container_width=True):
                for label, target_name, uid in sense_targets:
                    final_cmd = "look_out" if target_name == "out" else f"look {target_name}".strip()
                    if st.button(label, key=f"s_look_{uid}", use_container_width=True):
                        execute_sidebar_cmd(final_cmd)
        with i_r1_c2:
            with st.popover("👂 Hear", use_container_width=True):
                for label, target_name, uid in sense_targets:
                    if st.button(label, key=f"s_listen_{uid}", use_container_width=True):
                        execute_sidebar_cmd(f"listen {target_name}".strip())

        # Row 2: Smell | Touch
        i_r2_c1, i_r2_c2 = st.columns(2)
        with i_r2_c1:
            with st.popover("👃 Smell", use_container_width=True):
                for label, target_name, uid in sense_targets:
                    if st.button(label, key=f"s_smell_{uid}", use_container_width=True):
                        execute_sidebar_cmd(f"smell {target_name}".strip())
        with i_r2_c2:
            with st.popover("✋ Touch", use_container_width=True):
                for label, target_name, uid in sense_targets:
                    if st.button(label, key=f"s_touch_{uid}", use_container_width=True):
                        execute_sidebar_cmd(f"touch {target_name}".strip())
                    
        # Row 3: Taste | Say
        i_r3_c1, i_r3_c2 = st.columns(2)
        with i_r3_c1:
            with st.popover("👄 Taste", use_container_width=True):
                for label, target_name, uid in sense_targets:
                    if st.button(label, key=f"s_taste_{uid}", use_container_width=True):
                        execute_sidebar_cmd(f"taste {target_name}".strip())
        with i_r3_c2:
            with st.popover("💬 Say", use_container_width=True):
                txt = st.text_input("Say what?", key="pop_say", label_visibility="collapsed")
                if st.button("Speak", use_container_width=True) and txt: execute_sidebar_cmd(f"say {txt}")

        # Row 4: Pose | AI Outfits
        i_r4_c1, i_r4_c2 = st.columns(2)
        with i_r4_c1:
            with st.popover("🎭 Pose", use_container_width=True):
                txt = st.text_input("Pose?", key="pop_pose", label_visibility="collapsed")
                if st.button("Emote", use_container_width=True) and txt: execute_sidebar_cmd(f":{txt}")
        with i_r4_c2:
            # 👔 AI Outfits (Wizard OR Robot Owner)
            # Check for owned robots in the vicinity or generally?
            # render_outfit_manager(mode='others') checks ALL objects in DB for ownership.
            # So simple check: does player own ANY agents?
            owned_agents = [o for o in db.objects.values() if o.type == 'agent' and o.dbref != player.dbref and getattr(o, 'owner', '') == player_ref]
            has_permission = is_wiz or bool(owned_agents)
            
            if has_permission:
                with st.popover("👔 AI Outfits", help="AI Agent Outfits", use_container_width=True):
                    render_outfit_manager(player_ref, mode="others")
            else:
                st.button("👔❌", disabled=True, help="No Owned Robots", use_container_width=True)

    # 3. CONSTRUCTION SECTION
    with st.sidebar.expander("🛠️ Construction", expanded=False):
        render_construction_menu(player_ref)

    # 4. BELONGINGS SECTION
    with st.sidebar.expander("🎒 Belongings", expanded=False):
        # Row 1
        b_r1_c1, b_r1_c2 = st.columns(2)
        with b_r1_c1:
            with st.popover("🫴 Get", use_container_width=True):
                if room_objects:
                    for obj in room_objects:
                        if st.button(obj.name, key=f"g_{obj.dbref}", use_container_width=True):
                            execute_sidebar_cmd(f"get {obj.name}")
                else: st.caption("Nothing here.")
        with b_r1_c2:
            with st.popover("⬇️ Drop", use_container_width=True):
                if inventory:
                    for item in inventory:
                        if st.button(item.name, key=f"d_{item.dbref}", use_container_width=True):
                            execute_sidebar_cmd(f"drop {item.name}")
                else: st.caption("Empty.")
        
        # Row 2
        b_r2_c1, b_r2_c2 = st.columns(2)
        with b_r2_c1:
            with st.popover("🎁 Give", use_container_width=True):
                 targets = [a.name for a in others]
                 if not targets: st.caption("Nobody here.")
                 else:
                     target = st.selectbox("To:", targets, key="give_t")
                     tab_t, tab_i = st.tabs(["🪙", "📦"])
                     with tab_t:
                         amt = st.number_input("Amt", 1, value=10)
                         if st.button("Send", use_container_width=True): execute_sidebar_cmd(f"@give {target}={amt}")
                     with tab_i:
                         inv_names = [i.name for i in inventory]
                         if not inv_names: st.caption("Empty.")
                         else:
                             item = st.selectbox("Item:", inv_names)
                             if st.button("Give", use_container_width=True): execute_sidebar_cmd(f"@give {target}={item}")
        with b_r2_c2:
            with st.popover("👔 My Outfits", help="Your Outfits", use_container_width=True):
                 render_outfit_manager(player.dbref, mode="self")

    # 4. SYSTEM SECTION
    with st.sidebar.expander("⚙️ System", expanded=False):
        # Row 1: Online | Help (Tiled)
        sys_r1_c1, sys_r1_c2 = st.columns(2)
        with sys_r1_c1:
            if st.button("🌐 Online", help="Online List", use_container_width=True): 
                execute_sidebar_cmd("@who")
        with sys_r1_c2:
            if st.button("❓ Help", help="Help System", use_container_width=True):
                execute_sidebar_cmd("help")

        # Row 2: Snapshot | Purge Buffers (Tiled, Wizard Only)
        if is_wiz:
            sys_r2_c1, sys_r2_c2 = st.columns(2)
            with sys_r2_c1:
                if st.button("🎨 Snapshot", help="Snapshot Scene (Async)", use_container_width=True):
                    execute_sidebar_cmd("@snapshot")
            with sys_r2_c2:
                if st.button("🧹 Purge Buffers", help="Clear message buffers for all players", use_container_width=True):
                    execute_sidebar_cmd("@purge_buffer")

            # Row 3: Reload | Dump (Tiled, Wizard Only)
            sys_r3_c1, sys_r3_c2 = st.columns(2)
            with sys_r3_c1:
                if st.button("🔄 Reload", help="Clear cache and rerun UI", use_container_width=True):
                    st.cache_resource.clear()
                    st.rerun()
            with sys_r3_c2:
                if st.button("💾 Dump", help="Force save world state", use_container_width=True):
                    save_world(announce=True)
                    st.rerun()
            
            st.divider()
            
            # --- Artifact Configuration (Wizard Only) ---
            st.markdown("**📁 Path Configuration**")
            # Snapshot Path
            val_snap = st.text_input("Snapshot Dir", value=st.session_state.snapshot_path)
            if val_snap != st.session_state.snapshot_path:
                st.session_state.snapshot_path = val_snap
                st.rerun()
                
            # Research Path
            val_res = st.text_input("Research Dir", value=st.session_state.research_path)
            if val_res != st.session_state.research_path:
                st.session_state.research_path = val_res
                st.rerun()
        else:
            st.caption("*Advanced system tools require Wizard privileges.*")

    # 5. VR CONTROL SECTION (Wizard Only, in VR Rooms)
    if is_wiz and getattr(location, 'vr_ok', False):
        with st.sidebar.expander("🌀 VR Control", expanded=True):
            # Row 1: Reset | Clear
            vr_r1_c1, vr_r1_c2 = st.columns(2)
            with vr_r1_c1:
                if st.button("🔄 Reset", help="Reset your subjective reality", use_container_width=True):
                    execute_sidebar_cmd("@reset")
            with vr_r1_c2:
                if st.button("🧹 Clear", help="Wipe all VR state from room", use_container_width=True):
                    execute_sidebar_cmd("@vr_clear")
            
            # Text inputs for Context and Intent
            curr_memo = location.attrs.get('_vr_memo', '')
            new_memo = st.text_input("Context (The Program)", value=curr_memo, help="e.g. 'Cyberpunk Tokyo'", key="vr_memo_in")
            if new_memo != curr_memo:
                if st.button("Update Memo", use_container_width=True, key="btn_update_memo"):
                    execute_sidebar_cmd(f"@vr_memo {new_memo}")
            
            curr_intent = location.attrs.get('_vr_intent', '')
            new_intent = st.text_input("Intent (The Goal)", value=curr_intent, help="e.g. 'Find the hacker'", key="vr_intent_in")
            if new_intent != curr_intent:
                if st.button("Update Intent", use_container_width=True, key="btn_update_intent"):
                    execute_sidebar_cmd(f"@vr_intent {new_intent}")

    # --- RESEARCH STATUS ---
    engine = get_engine()
    job = getattr(engine, 'current_research_job', None)
    if job:
        with st.sidebar.expander("🧪 Active Research", expanded=True):
            status = job.get('status', 'UNKNOWN')
            topic = job.get('topic', 'Unknown Topic')
            if status == 'RUNNING':
                st.info(f"Working on: **{topic}**")
                st.caption("Job is running in background...")
            elif status == 'COMPLETED':
                st.success(f"Complete: {topic}")
                st.caption(f"Saved: `{os.path.basename(job.get('output_path'))}`")
                if st.button("Dismiss Result", use_container_width=True):
                    engine.current_research_job = None
                    st.rerun()
            elif status == 'FAILED':
                st.error(f"Failed: {topic}")
                st.error(job.get('error', 'Unknown Error'))
                if st.button("Dismiss Error", use_container_width=True):
                    engine.current_research_job = None
                    st.rerun()

    # --- SNAPSHOT STATUS (Visual Loom) ---
    snap_job = getattr(engine, 'current_snapshot_job', None)
    if snap_job:
        with st.sidebar.expander("🎨 Visual Loom", expanded=True):
            s_status = snap_job.get('status', 'UNKNOWN')
            if s_status == 'RUNNING':
                st.info("🎨 **Blooming Scene...**")
                st.caption("Synthesizing high-fidelity snapshot...")
            elif s_status == 'COMPLETED':
                st.success("🎨 **Bloom Complete**")
                st.caption(f"Saved: `{os.path.basename(snap_job.get('output_path'))}`")
                if st.button("Dismiss & Refresh Gallery", use_container_width=True):
                    engine.current_snapshot_job = None
                    st.session_state.gallery_index = 0 # Reset to show latest
                    st.rerun()
            elif s_status == 'FAILED':
                st.error("🎨 **Bloom Failed**")
                st.error(snap_job.get('error', 'Unknown Error'))
                if st.button("Dismiss Error", key="dismiss_snap_err", use_container_width=True):
                    engine.current_snapshot_job = None
                    st.rerun()

    # AI Minds (Robots + VR Rooms)
    ai_agents = [a for a in room_contents if a.autonomous]
    
    # Check if current location is a VR room
    vr_room = None
    if getattr(location, 'vr_ok', False):
        vr_room = location
    
    # Show section if we have AI agents OR VR room
    if ai_agents or vr_room:
        with st.sidebar.expander("🧠 AI Minds", expanded=False):
            # 2-Column Grid Layout
            mind_cols = st.columns(2)
            item_index = 0
            
            # VR Room first (if applicable)
            if vr_room:
                with mind_cols[item_index % 2]:
                    with st.popover(f"🌀 {vr_room.name}", use_container_width=True):
                        st.markdown(f"### {vr_room.name} (VR)")
                        st.caption("Virtual Reality Environment")
                        
                        st.divider()
                        
                        # VR Memory (Room-level program)
                        st.markdown("**🧠 VR Memory (The Program)**")
                        vr_memo = vr_room.attrs.get('_vr_memo', '')
                        if vr_memo:
                            st.info(vr_memo)
                        else:
                            st.caption("*No VR program set.*")
                            
                        # VR Intent (Room-level goal)
                        st.markdown("**⚡ VR Intent (The Goal)**")
                        vr_intent = vr_room.attrs.get('_vr_intent', '')
                        if vr_intent:
                            st.warning(vr_intent)
                        else:
                            st.caption("*No VR goal set.*")
                        
                        # Player's current VR state
                        st.markdown("**👁️ Your Current Reality**")
                        vr_desc_key = f"_vr_desc_{player.dbref}"
                        player_vr = vr_room.attrs.get(vr_desc_key, '')
                        if player_vr:
                            st.success(player_vr[:200] + "..." if len(player_vr) > 200 else player_vr)
                        else:
                            st.caption("*No subjective reality yet. Try exploring!*")
                        
                        st.divider()
                        
                        # VR Controls (Owner or Wizard)
                        can_edit = is_wiz or getattr(vr_room, 'owner', '') == player.dbref
                        if can_edit:
                            st.caption("**VR Controls:**")
                            ctrl_cols = st.columns(2)
                            with ctrl_cols[0]:
                                if st.button("🔄 Reset", key=f"vr_reset_{vr_room.dbref}", use_container_width=True):
                                    execute_sidebar_cmd("@reset")
                            with ctrl_cols[1]:
                                if st.button("🧹 Clear All", key=f"vr_clear_{vr_room.dbref}", use_container_width=True):
                                    execute_sidebar_cmd(f"@vr_clear {vr_room.dbref}")
                        else:
                            st.caption("*VR controls require ownership.*")
                item_index += 1
            
            # AI Agents
            for ai in ai_agents:
                with mind_cols[item_index % 2]:
                    # Popover for each agent
                    with st.popover(f"🤖 {ai.name}", use_container_width=True):
                        st.markdown(f"### {ai.name}")
                        st.caption(ai.desc)
                        
                        st.divider()
                        
                        # Memory (Memo)
                        st.markdown("**🧠 Memory (Static Facts)**")
                        memo = getattr(ai, 'memo', '')
                        if memo:
                            st.info(memo)
                        else:
                            st.caption("*No persistent memories.*")
                            
                        # Intent (Status/Upsum)
                        st.markdown("**⚡ Intent (Current Goal)**")
                        status = getattr(ai, 'status', 'None')
                        if status:
                            st.warning(status)
                        else:
                            st.caption("*No active intent.*")
                        
                        st.divider()
                        
                        # Active Probe (Wizard Only)
                        if is_wiz:
                            if st.button(f"🔮 Probe Mind", key=f"pr_{ai.dbref}", use_container_width=True):
                                execute_sidebar_cmd(f"@mind {ai.name}")
                        else:
                            st.caption("*Probe requires Wizard privileges.*")
                item_index += 1




# Restore global call
render_sidebar()


# ─────────────────────────────────────────────────────────────────
# Main Chat Interface
# ─────────────────────────────────────────────────────────────────

st.title("🌐 MASH")
st.caption("Multi-Agent Semantic Hallucination")

# Main View Controller
view = st.session_state.get("main_view_mode", "Chat")

# Reset specific views on entry
if view == "Snapshot" and st.session_state.get("last_view") != "Snapshot":
    st.session_state.gallery_index = 0

# Update tracking
st.session_state.last_view = view


if view == "Chat":
    # Display chat history
    for msg in st.session_state.messages:
        if msg["role"] == "system":
            st.info(msg["content"])
        elif msg["role"] == "user":
            with st.chat_message("user"):
                st.markdown(f"`> {msg['content']}`")
        else:  # assistant
            with st.chat_message("assistant", avatar="🌐"):
                st.markdown(msg["content"])


    # Input has been moved to global scope (bottom of file) for sticky behavior.
    # See End of File.

elif view == "Snapshot":
    st.markdown("### 🖼️ Snapshot Gallery")
    
    # Path validation/listing
    snapshot_path = Path(st.session_state.snapshot_path)
    if snapshot_path.exists() and snapshot_path.is_dir():
        # Get all images, sorted by name (timestamp descending)
        image_files = sorted(
            [f for f in snapshot_path.iterdir() if f.suffix.lower() in ('.png', '.jpg', '.jpeg')],
            key=lambda x: x.name.lower(),
            reverse=True
        )

        
        if image_files:
            # Layout: Margin | Left (20%) | Main (40%) | Right (20%) | Margin
            # Ratios [1, 2, 4, 2, 1] sums to 10.
            # 1/10=10% Margin, 2/10=20% Side, 4/10=40% Main.
            _, col_prev, col_main, col_next, _ = st.columns([1, 2, 4, 2, 1])
            
            # Ensure index is in range
            idx = st.session_state.gallery_index
            N = len(image_files)
            if idx >= N: 
                idx = 0
                st.session_state.gallery_index = 0
            
            # Circular indices
            idx_left = (idx + 1) % N
            idx_right = (idx - 1 + N) % N
            
            with col_prev:
                # Navigation Button
                if st.button("⬅️", key="btn_prev", width="stretch"):
                    st.session_state.gallery_index = idx_left
                    st.rerun()
                
                # Spacer (~20% vertical)
                for _ in range(5): st.write("")
                
                # Preview Image
                st.image(str(image_files[idx_left]), width="stretch")
                st.caption(f"{idx_left+1}/{N}")

            # --- Main (Center) Column ---
            with col_main:
                selected_img = image_files[idx]
                st.image(str(selected_img), width="stretch")
                # Metadata under main image
                st.markdown(f"<center><b>{idx+1} / {N}</b></center>", unsafe_allow_html=True)
                st.caption(f"📅 {selected_img.name}")

            # --- Next (Right) Column ---
            with col_next:
                # Navigation Button
                if st.button("➡️", key="btn_next", width="stretch"):
                    st.session_state.gallery_index = idx_right
                    st.rerun()
                    
                # Spacer (~20% vertical)
                for _ in range(5): st.write("")
                    
                st.image(str(image_files[idx_right]), width="stretch")
                st.caption(f"{idx_right+1}/{N}")
            
            with st.expander("📝 Metadata"):
                st.write(f"**Path:** `{selected_img}`")
                st.write(f"**Size:** {round(selected_img.stat().st_size / 1024, 2)} KB")
        else:
            st.info(f"No snapshots found in `{st.session_state.snapshot_path}`. Generate one to start your gallery!")
    else:
        st.error(f"Snapshot directory not found: `{st.session_state.snapshot_path}`")
        st.info("Check your 'Artifact Config' in the sidebar.")

elif view == "Research":
    st.markdown("### 📚 Research Archives")
    
    # Research Artifacts Path (Customizable)
    research_path = Path(st.session_state.get("research_path", "research_artifacts"))
    
    if research_path.exists() and research_path.is_dir():
        # Get all markdown files, sorted by name (timestamp decending/ascending dependent on name format)
        # Using modification time might be safer for "newest first"
        md_files = sorted(
            [f for f in research_path.iterdir() if f.suffix.lower() == '.md'],
            key=lambda x: x.stat().st_mtime,
            reverse=True
        )
        
        if md_files:
            # Reusing the 1-2-4-2-1 layout
            _, col_prev, col_main, col_next, _ = st.columns([1, 2, 8, 2, 1])
            
            # Ensure index is in range
            idx = st.session_state.research_index
            N = len(md_files)
            if idx >= N: 
                idx = 0
                st.session_state.research_index = 0
            
            # Circular indices
            idx_left = (idx + 1) % N
            idx_right = (idx - 1 + N) % N
            
            # --- Previous (Left) ---
            with col_prev:
                if st.button("⬅️", key="btn_res_prev", use_container_width=True):
                    st.session_state.research_index = idx_left
                    st.rerun()
                # Metadata Preview
                selected_prev = md_files[idx_left]
                st.caption(f"Prev: {selected_prev.name[:15]}...")

            # --- Next (Right) ---
            with col_next:
                if st.button("➡️", key="btn_res_next", use_container_width=True):
                    st.session_state.research_index = idx_right
                    st.rerun()
                selected_next = md_files[idx_right]
                st.caption(f"Next: {selected_next.name[:15]}...")
            
            # --- Main Display ---
            with col_main:
                selected_file = md_files[idx]
                st.info(f"📄 **{selected_file.name}** | Size: {round(selected_file.stat().st_size / 1024, 2)} KB")
                
                with open(selected_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                # Scrollable container for the markdown content
                with st.container(height=600):
                    st.markdown(content)
                    
            # Footer Metadata
            st.divider()
            st.caption(f"Archive {idx+1} of {N} | Located at: `{selected_file.absolute()}`")
            
        else:
            st.info("No research artifacts found. Use `@deep_research <topic>` to generate one!")
    else:
        st.warning(f"Research directory not found: `{research_path.absolute()}`")


# ─────────────────────────────────────────────────────────────────
# 7. Global Command Input (Sticky Footer)
# ─────────────────────────────────────────────────────────────────
# Placed here (outside tabs) so it remains pinned to the bottom of the viewport.

if prompt := st.chat_input("Type a command..."):
    # ═══════════════════════════════════════════════════════════════
    # SCRIPT MODE: State-Machine Parser for Multi-Line Inputs
    # ═══════════════════════════════════════════════════════════════
    # Use [] to group multi-line commands (e.g. [memo\n- Item 1\n- Item 2])
    
    commands = parse_input_stream(prompt)
    
    if not commands:
        st.rerun()  # Nothing to do
    
    # Process each command in sequence
    engine = get_engine()
    db = get_db()
    player = db.get_agent(st.session_state.player_ref)
    
    for cmd in commands:
        # 1. Immediately log the command to UI (unless silent meta-command)
        if not cmd.startswith(('&', '@memo', '@status', '@upsum')):
             st.session_state.messages.append({"role": "user", "content": f"**{player.name}:** {cmd}"})
             
        # 2. Check for auto-save (Now synchronous and safe!)
        check_auto_save()
        
        # Refresh player reference in case location/tokens changed
        player = db.get_agent(st.session_state.player_ref)
        
        # 3. Process the command
        # Check for wizard commands first
        response_msg = handle_wizard_command(st.session_state.player_ref, cmd)
        
        if not response_msg:
            # Process regular command
            result = engine.process_command(st.session_state.player_ref, cmd)
            response_msg = result.message
        else:
            # Fake a result object for wizard commands to simplify logic
            from types import SimpleNamespace
            result = SimpleNamespace(success=True, message=response_msg, context={'category': 'System'})

        is_system = (result.context.get('category') == 'System') if (result.context and isinstance(result.context, dict)) else False

        if is_system:
            if response_msg:
                st.toast(response_msg)
                # Also add to history so user definitely sees it
                st.session_state.messages.append({"role": "assistant", "content": f"🛠️ **System:** {response_msg}"})
        else:
            # Regular commands: ALWAYS show the direct response
            if response_msg:
                st.session_state.messages.append({"role": "assistant", "content": response_msg})
            
            
            # 4. IMMEDIATE REACTION (The "Tick")
            # We process reactions NOW, before the rerun, so they appear instantly.
            if result.success and player:
                # Check for "Leaving" trigger (Off-screen) first
                old_loc = result.context.get('from_room', {}).get('dbref') if result.context else None
                if old_loc and old_loc != player.location:
                    off_res = engine.trigger_room_reactions(old_loc, player.dbref, f"PRESENCE_DEPARTURE {player.name}")
                    for r in off_res:
                        if r['narrative'] and not is_near_duplicate(r['narrative'], st.session_state.messages):
                            st.session_state.messages.append({"role": "assistant", "content": f"✨ {r['narrative']}"})

                # Trigger reaction in CURRENT room
                with st.spinner("..."):
                    reactions = engine.trigger_room_reactions(player.location, player.dbref, cmd)
                
                for r in reactions:
                    if r['narrative'] and not is_near_duplicate(r['narrative'], st.session_state.messages):
                        st.session_state.messages.append({"role": "assistant", "content": f"✨ {r['narrative']}"})
                    for msg in r.get('intent_messages', []):
                        if msg and not is_near_duplicate(msg, st.session_state.messages):
                            st.session_state.messages.append({"role": "assistant", "content": msg})

        # Poll for any buffered announcements (including those from reactions)
        if player:
            if hasattr(player, 'message_buffer') and player.message_buffer:
                for ann in player.message_buffer:
                    if not is_near_duplicate(ann, st.session_state.messages):
                        st.session_state.messages.append({"role": "assistant", "content": ann})
                player.message_buffer = [] # Clear buffer
                db.save(WORLD_FILE) # Persist the clear immediately

    
    # Check for auto-save after script
    check_auto_save()
    
    # Update sidebar and chat by rerunning
    st.rerun()

# ─────────────────────────────────────────────────────────────────
# 8. Background Synchronization
# ─────────────────────────────────────────────────────────────────
# Run the background polling loop at the very end of the script.
# This ensures it's registered after all UI elements are rendered.
with st.container():
    sync_poll_loop()




