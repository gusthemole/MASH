"""
MASH Engine
===========
Core game loop and command dispatch.

This is the 'dumb orchestrator' - it parses commands, updates state,
and calls out to the AI layer for descriptions and NPC actions.
"""

import random
import json
import re
import threading
import os
from datetime import datetime
from typing import Optional, Callable, Dict, List, Any
from dataclasses import dataclass, field, asdict
from database import WorldDatabase, GameObject


# ─────────────────────────────────────────────────────────────────
# Economy Configuration
# ─────────────────────────────────────────────────────────────────

COST_DIG = 50       # Cost to create a room (@dig)
COST_CREATE = 10    # Cost to create an object (@create)
COST_LINK = 10      # Cost to create/link an exit (@link)
COST_AGENT = 500    # Cost to create an NPC agent (@agent) - premium!


# Token drops when moving (tiered lottery system)
# 10% = 1 token, 5% = 5 tokens, 1% = 10 tokens (jackpot!)
TOKEN_DROP_TIERS = [
    (0.01, 10, "🎰 JACKPOT! You found **10 Tokens**!"),
    (0.05, 5,  "✨ Lucky! You found **5 Tokens**!"),
    (0.10, 1,  "✨ You found **1 Token**."),
]

STARTING_TOKENS = 100  # New players start with tokens to build


@dataclass
class CommandResult:
    """Result of executing a command."""
    success: bool
    message: str
    message_3p: Optional[str] = None # Third-person version for room logs
    # For AI layer to generate prose from
    context: Optional[Dict] = None


class MashEngine:
    """
    The MASH game engine - a dumb orchestrator.
    
    Responsibilities:
    - Parse player input into commands
    - Dispatch commands to handlers
    - Maintain game state via WorldDatabase
    - Provide hooks for AI layer (description generation, NPC actions)
    """
    
    def __init__(self, db: WorldDatabase, ai_engine: Optional[Any] = None):
        self.db = db
        self.ai = ai_engine
        self.commands: Dict[str, Callable] = {}
        self.command_meta: Dict[str, dict] = {}  # name -> {help, category, aliases, usage}
        self.function_meta: Dict[str, dict] = {} # name -> {help, usage}
        self.placeholder_meta: Dict[str, str] = {} # code -> help
        self.history: List[Dict[str, str]] = []  # Last N command/response pairs
        self.max_history = 20
        self.shortcuts: Dict[str, str] = {
            '"': 'say',   # "hello -> say hello
            ':': 'pose',  # :waves -> pose waves
        }
        
        # Deep Research State
        self.research_lock = threading.Lock()
        self.current_research_job: Optional[Dict[str, Any]] = None # {actor, topic, status, start_time}
        self.research_path = "research_artifacts"

        # Snapshot State
        self.snapshot_lock = threading.Lock()
        self.current_snapshot_job: Optional[Dict[str, Any]] = None # {actor, prompt, status, start_time}
        self.snapshot_path = "snapshots"
        
        self._register_builtins()
    
    def _register_builtins(self):
        """Register built-in commands."""
        # Movement
        self.register_command('go', self._cmd_go,
            category='Movement', usage='go <exit>', help='Move through an exit')
        self.register_command('enter', self._cmd_enter,
            category='Movement', usage='enter <object>', help='Enter an object-container')
        self.register_command('exit', self._cmd_exit,
            aliases=['leave'], category='Movement', help='Exit the current container')
        self.register_command('exits', self._cmd_exits,
            category='Movement', help='List exits with dbrefs')
        self.register_command('get', self._cmd_get,
            aliases=['take'], category='Movement', usage='get <object>', help='Pick up an object')
        self.register_command('drop', self._cmd_drop,
            category='Movement', usage='drop <object>', help='Drop an object')
        self.register_command('home', self._cmd_home,
            category='Movement', help='Return to your home room')
        
        # Senses
        self.register_command('look', self._cmd_look, 
            aliases=['l', 'read'], category='Senses', usage='look [target]', help='Look at room or object')
        self.register_command('look_out', self._cmd_look_out,
            aliases=['view', 'gaze'], category='Senses', usage='look_out', help='Look outside from inside a vehicle/container.')

        self.register_command('smell', self._cmd_smell,
            category='Senses', usage='smell [target]', help='Smell the room or target')
        self.register_command('taste', self._cmd_taste,
            category='Senses', usage='taste <target>', help='Taste something')
        self.register_command('touch', self._cmd_touch,
            aliases=['feel'], category='Senses', usage='touch <target>', help='Touch something')
        self.register_command('listen', self._cmd_listen,
            aliases=['hear'], category='Senses', usage='listen [target]', help='Listen to room or something')
        
        # Communication
        self.register_command('say', self._cmd_say,
            category='Communication', usage='say <text>', help='Say something (also: "text)')
        self.register_command('pose', self._cmd_pose,
            category='Communication', usage='pose <action>', help='Describe action (also: :action)')
        self.register_command('@emit', self._cmd_emit,
            aliases=['emit'], category='Communication', usage='@emit <text>', help='Send raw text to room')
        
        # Economy
        self.register_command('inventory', self._cmd_inventory,
            aliases=['i', 'inv'], category='Economy', help='Check tokens and items')
        self.register_command('@tokens', self._cmd_tokens,
            category='Economy', help='Check token balance')
        self.register_command('@give', self._cmd_give,
        category='Economy', usage='@give <player>=<amount>', help='Give tokens to player')
        self.register_command('give', self._cmd_give_item,
            category='Economy', usage='give <object> to <player>', help='Give an item to someone')
        
        # Building
        self.register_command('@dig', self._cmd_dig,
            category='Building', usage='@dig <room name>', help='Create room (10 tokens)')
        self.register_command('@create', self._cmd_create,
            category='Building', usage='@create <object>', help='Create object (1 token)')
        self.register_command('@agent', self._cmd_create_agent,
            category='Building', usage='@agent <name>', help='Create autonomous NPC agent (5 tokens). Can be robot, vehicle, etc.')
        self.register_command('@link', self._cmd_link,
            category='Building', usage='@link <exit>=<#dbref>', help='Link exit to room')
        self.register_command('@describe', self._cmd_describe,
            category='Building', usage='@describe <target>=<description>', help='Set description')
        self.register_command('@name', self._cmd_name,
            aliases=['@rename'],
            category='Building', usage='@name <target>=<new name>', help='Rename object')
        self.register_command('@destroy', self._cmd_destroy,
            aliases=['@delete', '@nuke'],
            category='Building', usage='@destroy <target>', help='Permanently delete an object and get a token refund.')
        self.register_command('@smell', self._cmd_set_smell,
            category='Building', usage='@smell <target>=<text>', help='Set smell')
        self.register_command('@taste', self._cmd_set_taste,
            category='Building', usage='@taste <target>=<text>', help='Set taste')
        self.register_command('@touch', self._cmd_set_touch,
            category='Building', usage='@touch <target>=<text>', help='Set texture')
        self.register_command('@listen', self._cmd_set_listen,
            category='Building', usage='@listen <target>=<text>', help='Set sound')
        
        self.register_command('@status', self._cmd_status,
            aliases=['@upsum', 'upsum'],
            category='Building', usage='@status [target=]<text>', help='Set narrative goal/intent (UPSUM). Use target= for robots.')
        self.register_command('@memo', self._cmd_memo,
            category='Building', usage='@memo [target=]<text>', help='Set persistent facts/preferences. Use target= for robots.')
        self.register_command('@wipe', self._cmd_wipe_memory,
            category='Building', usage='@wipe <target>', help='Wipe all memory and intent from a robot you own.')
        self.register_command('@mind', self._cmd_mind_read,
            aliases=['@probe', 'mind'],
            category='Senses', usage='@mind <target>', help='Silent Divinity: Read the persistent facts and current intent of an agent (Wizard only).')
        self.register_command('@purge_buffers', self._cmd_purge_buffers,
            aliases=['@purge_buffer'],
            category='Senses', help='Wizard only: Clear all agent message buffers to stop ghost echoes.')
        
        # AI Reaction Triggers
        self.register_command('@adesc', self._cmd_set_adesc,
            category='Building', usage='@adesc <target>=<instructions>', help='AI reaction to look')
        self.register_command('@asmell', self._cmd_set_asmell,
            category='Building', usage='@asmell <target>=<instructions>', help='AI reaction to smell')
        self.register_command('@ataste', self._cmd_set_ataste,
            category='Building', usage='@ataste <target>=<instructions>', help='AI reaction to taste')
        self.register_command('@atouch', self._cmd_set_atouch,
            category='Building', usage='@atouch <target>=<instructions>', help='AI reaction to touch')
        self.register_command('@alisten', self._cmd_set_alisten,
            category='Building', usage='@alisten <target>=<instructions>', help='AI reaction to listen')
        
        self.register_command('@set', self._cmd_set,
            category='Building', usage='@set <obj>/<attr>=<val>', help='Set a custom attribute. Use &ATTR target=val as a shortcut.')
        self.register_command('@enter_ok', self._cmd_set_enter_ok,
            category='Building', usage='@enter_ok <object>=<yes|no>', help='Toggle enterable status')
        self.register_command('@ai_ok', self._cmd_set_ai_ok,
            category='Building', usage='@ai_ok <object>=<yes|no>', help='Toggle AI generative reactions')
        self.register_command('@robot', self._cmd_set_robot,
            category='Building', usage='@robot <agent>=<yes|no>', help='Toggle AI control of agent')
        self.register_command('@listening', self._cmd_set_listening,
            category='Building', usage='@listening <object>=<yes|no>', help='Toggle object listening status. Essential for $ and ^ triggers to work.')
        self.register_command('@vehicle', self._cmd_set_vehicle,
            category='Building', usage='@vehicle <object>=<type>', help='Set vehicle type (boat, aircraft, mech, etc.) for vehicle locks.')
        
        # Ownership & Locks
        self.register_command('@lock', self._cmd_lock,
            category='Ownership', usage='@lock <target>=<lock>', help='Lock object (dbref, flag, object:#)')
        self.register_command('@unlock', self._cmd_unlock,
            category='Ownership', usage='@unlock <target>', help='Remove lock from object')
        self.register_command('@chown', self._cmd_chown,
            category='Ownership', usage='@chown <target>=<player>', help='Transfer ownership (wizard)')
        self.register_command('@home', self._cmd_at_home,
            category='Ownership', usage='@home <target>=<#dbref>', help="Set an target's home room")
        self.register_command('examine', self._cmd_examine,
            aliases=['ex'], category='Ownership', usage='examine <target>', help='Detailed object info')
        
        # System
        # AI Flags
        self.register_command('@search_ok', self._cmd_set_search_ok,
            category='Building', usage='@search_ok <target>=<true/false>', help='Enable Google Search Grounding')
        self.register_command('@summon_ok', self._cmd_set_summon_ok,
            category='Building', usage='@summon_ok <target>=<yes|no>', help='Allow agent to be summoned')
        
        # Summoning
        self.register_command('@summon', self._cmd_summon,
            aliases=['summon'], category='Movement', usage='@summon <agent>', help='Summon a willing agent to your location')
            
        # Outfits
        self.register_command('@outfit', self._cmd_outfit,
            category='System', usage='@outfit define <1-10>=<desc> | list', help='Manage outfits')
        self.register_command('@wear', self._cmd_wear,
            category='System', usage='@wear <1-10>', help='Wear a defined outfit')

        # Deep Research Command
        self.register_command('@deep_research', self._cmd_deep_research,
            aliases=['deep_research', 'deep'], category='System', usage='@deep_research <topic>', help='Start a deep research background job (100 Tokens)')

        # Snapshot Command (Async)
        self.register_command('@snapshot', self._cmd_snapshot,
            aliases=['snapshot'], category='System', help='Synthesize a high-fidelity image of the current scene (50 Tokens)')
            
        # Generic Set
        self.register_command('@set', self._cmd_set,
            category='Building', usage='@set <target>/<attr>=<value>', help='Set custom attribute')
        self.register_command('@tel', self._cmd_teleport,
            aliases=['@teleport'], category='Movement', usage='@tel <target>=<#dest>', help='Teleport object/self to destination (wizard)')

        self.register_command('help', self._cmd_help,
            category='System', usage='help [command]', help='Show help')
        self.register_command('@who', self._cmd_who,
            category='System', help='List all players online')
        
        # Math Commands
        self.register_command('@add', self._cmd_math_add,
            category='System', usage='@add <a> <b>', help='Add two numbers')
        self.register_command('@subtract', self._cmd_math_subtract,
            category='System', usage='@subtract <a> <b>', help='Subtract b from a')
        self.register_command('@multiply', self._cmd_math_multiply,
            category='System', usage='@multiply <a> <b>', help='Multiply two numbers')
        self.register_command('@divide', self._cmd_math_divide,
            category='System', usage='@divide <a> <b>', help='Divide a by b')
        
        # Date/Time Commands
        self.register_command('@date', self._cmd_date,
            category='System', help='Show current date')
        self.register_command('@time', self._cmd_time,
            category='System', help='Show current time')
        
        # VR Commands (Subjective Reality - Owner Controlled)
        self.register_command('@vr_ok', self._cmd_set_vr_ok,
            category='VR', usage='@vr_ok <room>=<yes|no>', help='Enable/disable VR mode for a room you own.')
        self.register_command('@reset', self._cmd_reset_vr,
            aliases=['@vr_reset'],
            category='VR', usage='@reset', help='Reset the current room VR overlay to base reality.')
        self.register_command('@vr_memo', self._cmd_vr_memo,
            category='VR', usage='@vr_memo [room=]<text>', help='Set VR context for a room (e.g., "Cyberpunk Tokyo").')
        self.register_command('@vr_intent', self._cmd_vr_intent,
            category='VR', usage='@vr_intent [room=]<text>', help='Set narrative goal for VR (e.g., "Find the hacker").')
        self.register_command('@vr_clear', self._cmd_vr_clear,
            category='VR', usage='@vr_clear [room]', help='Wipe ALL VR state from a room (desc, memo, intent).')
            
        # Programmable Functions
        self.register_function('rand', '[rand(n)]', 'Returns a random integer from 0 to n-1.')
        self.register_function('pick', '[pick(list, sep)]', 'Random element from delimited list (default sep is |).')
        self.register_function('v', '[v(attr)]', 'Value of a custom attribute on the speaker.')
        self.register_function('get', '[get(target/attr)]', 'Value of an attribute on a target object (e.g. #101/ball).')
        
        # Math Functions
        self.register_function('add', '[add(a, b)]', 'Add two numbers.')
        self.register_function('sub', '[sub(a, b)]', 'Subtract b from a.')
        self.register_function('mul', '[mul(a, b)]', 'Multiply two numbers.')
        self.register_function('div', '[div(a, b)]', 'Divide a by b.')
        
        # Date/Time Functions
        self.register_function('date', '[date()]', 'Current date (e.g., Friday, January 10, 2026).')
        self.register_function('time', '[time()]', 'Current time (e.g., 7:45 PM).')
        self.register_function('datetime', '[datetime()]', 'Current date and time.')
        self.register_function('math', '[math(expr)]', 'Safe evaluation of a math expression (e.g. math(10 * (5+2))).')
        
        # Placeholders
        self.register_placeholder('%n', "Trigger's Name (who caused the action)")
        self.register_placeholder('%!', "Actor's Name (the person speaking)")
        self.register_placeholder('%l', "Location Name")
        self.register_placeholder('%#', "Trigger's DBRef")
    
    def register_command(self, name: str, handler: Callable, 
                         aliases: List[str] = None, category: str = 'Other',
                         usage: str = None, help: str = ''):
        """Register a command handler with metadata for auto-documentation."""
        name_lower = name.lower()
        self.commands[name_lower] = handler
        
        # Store metadata (only for primary name, not aliases)
        self.command_meta[name_lower] = {
            'name': name,
            'handler': handler,
            'aliases': aliases or [],
            'category': category,
            'usage': usage or name,
            'help': help
        }
        
        # Register aliases
        for alias in (aliases or []):
            self.commands[alias.lower()] = handler

    def register_function(self, name: str, usage: str, help: str):
        """Register a MUSH-style function with metadata."""
        self.function_meta[name.lower()] = {
            'name': name,
            'usage': usage,
            'help': help
        }

    def register_placeholder(self, code: str, help: str):
        """Register a placeholder code with help text."""
        self.placeholder_meta[code] = help
    
    # ─────────────────────────────────────────────────────────────
    # Ownership & Lock Helpers
    # ─────────────────────────────────────────────────────────────
    
    def is_owner(self, agent_ref: str, obj: GameObject) -> bool:
        """Check if agent owns an object. Wizards own everything."""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return False
        if getattr(agent, 'wizard', False):
            return True  # Wizards own everything
        return getattr(obj, 'owner', '') == agent_ref
    
    def can_modify(self, agent_ref: str, obj: GameObject) -> bool:
        """Check if agent can modify an object (owner or wizard)."""
        return self.is_owner(agent_ref, obj)
    
    def passes_lock(self, agent_ref: str, obj: GameObject) -> bool:
        """
        Check if agent passes an object's lock.
        
        Lock formats:
        - "" (empty) = unlocked, everyone passes
        - "#101" = only that player
        - "wizard" = only wizards
        - "!wizard" = everyone except wizards
        - "object:#55" = must be holding object #55
        """
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return False
        
        # Wizards bypass all locks
        if getattr(agent, 'wizard', False):
            return True
        
        lock = getattr(obj, 'lock', '') or ''
        
        # Empty lock = unlocked
        if not lock:
            return True
        
        # Player lock: #dbref
        if lock.startswith('#'):
            return agent_ref == lock
        
        # Negated flag: !flag
        if lock.startswith('!'):
            flag_name = lock[1:]
            return not getattr(agent, flag_name, False)
        
        # Object lock: object:#dbref (must be holding object)
        if lock.startswith('object:'):
            required_obj = lock.split(':')[1]
            return required_obj in getattr(agent, 'inventory', [])
        
        # Vehicle lock: vehicle:<type> (must be inside OR BE a vehicle of that type)
        if lock.startswith('vehicle:'):
            required_type = lock.split(':')[1].lower()
            
            # Check if the agent IS a vehicle of this type (for vehicle movement)
            agent_vehicle_type = getattr(agent, 'vehicle_type', '') or ''
            if agent_vehicle_type.lower() == required_type:
                return True
            
            # Check if agent is INSIDE a vehicle of this type
            location = self.db.get(agent.location)
            if location and location.type != 'room':
                vehicle_type = getattr(location, 'vehicle_type', '') or ''
                return vehicle_type.lower() == required_type
            return False

        
        # Flag lock: just the flag name
        return getattr(agent, lock, False)
    
    def get_owner_name(self, obj: GameObject) -> str:
        """Get the name of an object's owner."""
        owner_ref = getattr(obj, 'owner', '')
        if not owner_ref:
            return "Nobody"
        owner = self.db.get_agent(owner_ref)
        return owner.name if owner else owner_ref

    # ─────────────────────────────────────────────────────────────
    # Object Matching
    # ─────────────────────────────────────────────────────────────

    def match_object(self, agent_ref: str, target: str) -> Optional[GameObject]:
        """
        Smart object matching.
        Resolves: 'me', 'here', 'self', 'room', #dbref, exact name, partial name.
        Searches: current room contents, current room exits, player inventory.
        """
        if not target:
            return None
        
        target = target.lower().strip()
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return None
            
        # 1. Special keywords
        if target in ['here', 'room', 'current']:
            return self.db.get_room(agent.location)
        if target in ['me', 'self', 'myself']:
            return agent
            
        # 2. DBRef match
        if target.startswith('#'):
            return self.db.get(target)
            
        # 3. Exact Name Match (Priority using index)
        exact_match_ref = self.db._name_index.get(target)
        if exact_match_ref:
            obj = self.db.get(exact_match_ref)
            if obj:
                # Security/Context check: Is it in the room, in inventory, or an exit?
                in_room = obj.dbref in [o.dbref for o in self.db.get_room_contents(agent.location)]
                is_exit = obj.type == 'exit' and obj.source == agent.location
                in_inv = obj.dbref in agent.inventory
                
                if in_room or is_exit or in_inv:
                    return obj

        # Fallback to manual scans for aliases and partial matches
        room_contents = self.db.get_room_contents(agent.location)
        room_exits = self.db.get_room_exits(agent.location)

        # 4. Partial Name Match (Substring for objects, Prefix for exits)
        # Check room contents
        for obj in room_contents:
            if target in obj.name.lower():
                return obj
                
        # Check exits (prefix match is better for movement/navigation)
        for exit_obj in room_exits:
            if exit_obj.name.lower().startswith(target):
                return exit_obj
            for alias in exit_obj.aliases:
                if alias.lower().startswith(target):
                    return exit_obj
        
        # Check exits again (substring match for "latter part" typing)
        for exit_obj in room_exits:
            if target in exit_obj.name.lower():
                return exit_obj
                    
        # Check inventory
        for item_ref in agent.inventory:
            item = self.db.get(item_ref)
            if item and target in item.name.lower():
                return item
                
        return None

    # ─────────────────────────────────────────────────────────────
    
    def get_tokens(self, agent_ref: str) -> int:
        """Get token balance. Wizards have infinite."""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return 0
        if getattr(agent, 'wizard', False):
            return float('inf')  # Wizards have infinite tokens
        return getattr(agent, 'tokens', 0)
    
    def spend_tokens(self, agent_ref: str, amount: int) -> bool:
        """Spend tokens. Returns True if successful."""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return False
        if getattr(agent, 'wizard', False):
            return True  # Wizards spend nothing
        current = getattr(agent, 'tokens', 0)
        if current < amount:
            return False
        agent.tokens = current - amount
        return True
    
    def add_tokens(self, agent_ref: str, amount: int) -> int:
        """Add tokens to an agent. Returns new balance."""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return 0
        current = getattr(agent, 'tokens', 0)
        agent.tokens = current + amount
        return agent.tokens
    
    def maybe_drop_tokens(self, agent_ref: str) -> str:
        """Random chance to find tokens when moving. Tiered lottery system."""
        roll = random.random()
        cumulative = 0.0
        
        agent = self.db.get_agent(agent_ref)
        is_wiz = agent and getattr(agent, 'wizard', False)
        
        # DEBUG: Uncomment next line to force 100% drop for testing
        # roll = 0.001  # Force jackpot
        
        for chance, amount, message in TOKEN_DROP_TIERS:
            cumulative += chance
            if roll < cumulative:
                self.add_tokens(agent_ref, amount)
                balance_str = "∞" if is_wiz else str(self.get_tokens(agent_ref))
                return f"\n\n{message} (Balance: {balance_str})"
        
        return ""
    
    # ─────────────────────────────────────────────────────────────
    # Main Entry Point
    # ─────────────────────────────────────────────────────────────
    
    def process_command(self, agent_ref: str, raw_input: str, trigger_ref: str = None) -> CommandResult:
        """
        Process a command from an agent.
        
        trigger_ref: The dbref of the agent who triggered this (optional).
        Returns a CommandResult with the output message and context for AI.
        """
        raw_input = raw_input.strip()
        if not raw_input:
            return CommandResult(False, "")
            
        # Store trigger_ref for placeholder evaluation during this command
        self._current_trigger = trigger_ref
        
        # Pre-fetch agent to avoid UnboundLocalError later
        agent = self.db.get_agent(agent_ref)
        
        # Handle & shortcut for attributes (&ATTR target=value)
        if raw_input.startswith('&'):
            return self._cmd_set_shortcut(agent_ref, raw_input[1:].strip())
            
        # Handle shortcuts (" for say, : for pose)
        if raw_input[0] in self.shortcuts:
            cmd_name = self.shortcuts[raw_input[0]]
            args = raw_input[1:].strip()
        else:
            parts = raw_input.split(None, 1)
            cmd_name = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""
        
        # Look up command handler
        handler = self.commands.get(cmd_name)
        if not handler:
            # Check for bare exit (typing "north" instead of "go north")
            # Moved to fallback so commands like "look" don't match exits named "Overlook"
            agent = self.db.get_agent(agent_ref)
            if agent:
                # Use raw_input to check the FULL string (e.g. "Switch Red" vs "Switch")
                exit_obj = self.db.find_exit_by_name(agent.location, raw_input)
                if exit_obj:
                    res = self._cmd_go(agent_ref, exit_obj.dbref)
                else:
                    # SHADOW COMMANDS ($ Attributes)
                    res = self._match_dollar_commands(agent_ref, raw_input)
                    if not res:
                        # ---------------------------------------------------------
                        # VR ROOM INTERCEPTION (The Holodeck Hook)
                        # ---------------------------------------------------------
                        # If the room is VR-enabled, we treat ANY unknown command
                        # as a prompt to evolve the room's description.
                        # ---------------------------------------------------------
                        loc = self.db.get_room(agent.location)
                        if loc and getattr(loc, 'vr_ok', False) and self.ai:
                            # 1. Get subjective state (or base state)
                            vr_key = f"_vr_{agent.dbref}"
                            # We default to the room's base description if no VR state exists yet
                            current_desc = loc.attrs.get(vr_key, loc.desc)
                            
                            # 2. Evolve the room
                            # We pass the current DESCRIPTION and the TRIGGER (user input)
                            # The AI returns a new description.
                            context = {
                                'current_desc': current_desc,
                                'trigger': raw_input,
                                'agent_name': agent.name
                            }
                            
                            # Using a dedicated AI method for this "World Weaving"
                            new_desc = self.ai.evolve_room(context)
                            
                            if new_desc:
                                # 3. Save subjective state (Shadow DOM)
                                loc.attrs[vr_key] = new_desc
                                
                                # 4. Return the result (The user sees the change immediately)
                                # We treat this as a success.
                                res = CommandResult(True, new_desc)
                            else:
                                res = CommandResult(False, "The reality flickers but refuses to change.")
                        
                        if not res:
                             res = CommandResult(False, f"Unknown command: {cmd_name}")
        else:
            # Execute handler
            result = handler(agent_ref, args)
            
            # For autonomous agents, if we have a 3rd person message, use it as primary
            # This ensures the 'return' message is what everyone else sees
            if agent and agent.autonomous and result.success and result.message_3p:
                result.message = result.message_3p
                
            if result.success:
                self._add_to_history(agent_ref, cmd_name, result.message)
            res = result
            
            # ---------------------------------------------------------
            # VR EXPANSION: Failed "go" interception
            # ---------------------------------------------------------
            # If 'go' failed in a VR room, treat it as a narrative movement.
            if cmd_name == 'go' and not result.success:
                loc = self.db.get_room(agent.location) if agent else None
                if loc and getattr(loc, 'vr_ok', False) and self.ai:
                    # Per-player VR description (unique experience)
                    vr_desc_key = f"_vr_desc_{agent.dbref}"
                    current_desc = loc.attrs.get(vr_desc_key, loc.desc)
                    
                    # Room-level VR context (owner-controlled "program")
                    vr_memo = loc.attrs.get("_vr_memo", "")
                    vr_intent = loc.attrs.get("_vr_intent", "")
                    
                    context = {
                        'current_desc': current_desc,
                        'trigger': f"User walks towards: {args}",
                        'agent_name': agent.name,
                        'vr_memo': vr_memo,
                        'vr_intent': vr_intent
                    }
                    
                    new_desc = self.ai.evolve_room(context)
                    if new_desc:
                        # Capture any embedded [vr_desc] commands from AI
                        self.capture_robot_intent(agent_ref, new_desc)
                        # Strip commands from visible output
                        clean_desc = re.sub(r'\[vr_desc [^\]]+\]', '', new_desc).strip()
                        # Also store it directly (fallback)
                        if vr_desc_key not in loc.attrs:
                            loc.attrs[vr_desc_key] = clean_desc
                        res = CommandResult(True, clean_desc)
            
            # ---------------------------------------------------------
            # VR EXPANSION: Agentic reactions to pose/say
            # ---------------------------------------------------------
            # If pose or say succeeded in a VR room, trigger a reaction.
            if cmd_name in ['pose', 'say'] and result.success:
                loc = self.db.get_room(agent.location) if agent else None
                if loc and getattr(loc, 'vr_ok', False) and self.ai:
                    # Per-player VR description
                    vr_desc_key = f"_vr_desc_{agent.dbref}"
                    current_desc = loc.attrs.get(vr_desc_key, loc.desc)
                    
                    # Room-level VR context
                    vr_memo = loc.attrs.get("_vr_memo", "")
                    vr_intent = loc.attrs.get("_vr_intent", "")
                    
                    reaction_context = {
                        'current_desc': current_desc,
                        'user_action': result.message,
                        'agent_name': agent.name,
                        'vr_memo': vr_memo,
                        'vr_intent': vr_intent
                    }
                    
                    reaction = self.ai.react_to_vr(reaction_context)
                    if reaction:
                        # Check for [scene_change] signal - DM wants Architect to rebuild
                        if '[scene_change]' in reaction.lower():
                            # Chain to evolve_room
                            evolve_context = {
                                'current_desc': current_desc,
                                'trigger': f"Scene change requested: {result.message}",
                                'agent_name': agent.name,
                                'vr_memo': vr_memo,
                                'vr_intent': vr_intent
                            }
                            new_desc = self.ai.evolve_room(evolve_context)
                            if new_desc:
                                # Capture any embedded commands from Architect
                                self.capture_robot_intent(agent_ref, new_desc)
                                # Strip commands from visible output
                                clean_desc = re.sub(r'\[vr_desc [^\]]+\]', '', new_desc)
                                clean_desc = re.sub(r'\[vr_title [^\]]+\]', '', clean_desc)
                                clean_desc = re.sub(r'\[scene_change\]', '', clean_desc, flags=re.IGNORECASE).strip()
                                # Store fallback if not set by [vr_desc]
                                if vr_desc_key not in loc.attrs:
                                    loc.attrs[vr_desc_key] = clean_desc
                                # Strip scene_change from DM reaction too
                                clean_reaction = re.sub(r'\[scene_change\]', '', reaction, flags=re.IGNORECASE).strip()
                                res = CommandResult(True, f"{result.message}\n\n{clean_reaction}\n\n---\n\n{clean_desc}")
                            else:
                                # Fallback if evolve fails
                                clean_reaction = re.sub(r'\[scene_change\]', '', reaction, flags=re.IGNORECASE).strip()
                                res = CommandResult(True, f"{result.message}\n\n{clean_reaction}")
                        else:
                            # Normal DM reaction (no scene change)
                            self.capture_robot_intent(agent_ref, reaction)
                            clean_reaction = re.sub(r'\[vr_desc [^\]]+\]', '', reaction)
                            clean_reaction = re.sub(r'\[vr_title [^\]]+\]', '', clean_reaction)
                            clean_reaction = re.sub(r'\[scene_change\]', '', clean_reaction, flags=re.IGNORECASE).strip()
                            res = CommandResult(True, f"{result.message}\n\n{clean_reaction}")
        
        # --- AUTOMATED TICK LOGIC (for Robots) ---
        if agent and agent.robot and cmd_name == "tick":
            # Get AI context for autonomous action
            ctx = self.get_ai_context(agent_ref, agent_ref, "tick")
            ctx['memo'] = getattr(agent, 'memo', '')
            
            # Prepare search mode
            search_mode = None
            if getattr(agent, 'search_ok', False): search_mode = 'grounding'
            
            result_str = self.ai.get_robot_tick(ctx, search_mode=search_mode)
            
            # Extract internal thoughts vs command
            clean_thought = re.sub(r'\[.*?\]', '', result_str).strip()
            
            # Execute embedded commands
            intents = self.capture_robot_intent(agent_ref, result_str)
            intent_msgs = [r.message for r in intents if r.message]
            
            # Return combined result
            msg = ""
            if clean_thought:
                msg = f"*{agent.name} thinks:* {clean_thought}"
            
            return CommandResult(
                True, 
                msg, 
                context={'action': 'tick', 'intent_messages': intent_msgs}
            )
            
        # Add to history
        self._add_to_history(agent_ref, raw_input, res.message)
        
        # Update interaction timestamp (prevents idle timeouts)
        self.update_interaction(agent_ref)
        
        return res

    def _match_dollar_commands(self, agent_ref: str, raw_input: str) -> Optional[CommandResult]:
        """Check all listening objects in the room for $pattern:action matches."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return None
        
        # Collect all objects in the vicinity
        loc_contents = self.db.get_room_contents(agent.location)
        # Include the room itself, the player, and items in the room
        candidates = [self.db.get(agent.location)] + loc_contents
        
        import fnmatch
        
        for obj in candidates:
            if not obj or not getattr(obj, 'listening', False):
                continue
            
            for attr_name, attr_val in obj.attrs.items():
                if attr_val.startswith('$') and ':' in attr_val:
                    pattern, action = attr_val[1:].split(':', 1)
                    # Use fnmatch for case-insensitive glob matching
                    if fnmatch.fnmatch(raw_input.lower(), pattern.lower()):
                        # Wildcard extraction (simple version)
                        # We try to map words to %0..%9
                        words = raw_input.split()
                        pat_words = pattern.split()
                        
                        # Wildcard extraction: %0 is everything after the pattern prefix
                        expanded_action = action
                        if '*' in pattern:
                            expanded_action = expanded_action.replace('%0', raw_input)
                        
                        # Process the action through placeholders
                        # Executor is the object (obj), Trigger is the player (agent_ref)
                        final_action = self._substitute_placeholders(obj.dbref, expanded_action, agent_ref)
                        # Execute!
                        return self.process_command(obj.dbref, final_action, agent_ref)
        
        return None

    def _trigger_listen_patterns(self, agent_ref: str, text: str):
        """Broadcast text to all listening objects in the room for ^pattern:action matches."""
        agent = self.db.get(agent_ref)
        if not agent or not agent.location: return
        
        loc_contents = self.db.get_room_contents(agent.location)
        candidates = [self.db.get(agent.location)] + loc_contents
        
        import fnmatch
        
        for obj in candidates:
            if not obj or not getattr(obj, 'listening', False):
                continue
                
            for attr_name, attr_val in obj.attrs.items():
                if attr_val.startswith('^') and ':' in attr_val:
                    pattern, action = attr_val[1:].split(':', 1)
                    if fnmatch.fnmatch(text.lower(), pattern.lower()):
                        # Expand %0 as the full text heard
                        expanded_action = action.replace('%0', text)
                        # Executor is the object (obj), Trigger is the player (agent_ref)
                        final_action = self._substitute_placeholders(obj.dbref, expanded_action, agent_ref)
                        # Execute in background
                        self.process_command(obj.dbref, final_action, agent_ref)
        
    def _evaluate_functions(self, agent_ref: str, text: str, context_ref: str = None) -> str:
        """
        Evaluate MUSH-style functions [func(args)].
        Supported: rand(n), pick(list, sep), v(attr), get(target/attr), add, sub, mul, div, math(expr).
        """
        if not text or '[' not in text:
            return text
            
        def replacer(match):
            content = match.group(1).strip()
            if '(' not in content:
                return f"[{content}]" # Not a function call
            
            try:
                # Find the first '(' to separate function name
                split_idx = content.find('(')
                if split_idx == -1: return f"[{content}]"
                
                func_name = content[:split_idx].lower().strip()
                args_str = content[split_idx+1:].strip()
                
                # Balancing logic: Find the closing paren that matches the first open paren
                paren_depth = 1
                closing_idx = -1
                for i, char in enumerate(args_str):
                    if char == '(': paren_depth += 1
                    elif char == ')': paren_depth -= 1
                    
                    if paren_depth == 0:
                        closing_idx = i
                        break
                
                if closing_idx != -1:
                    # Capture everything before the matching paren
                    args_inside = args_str[:closing_idx]
                    # The rest is ignored or part of a larger string
                    args_str = args_inside
                elif args_str.endswith(')'):
                    # Fallback for simple cases if depth check fails
                    args_str = args_str[:-1]
                
                # Smart split by comma (ignore commas inside nested parens)
                args = []
                curr = []
                depth = 0
                for c in args_str:
                    if c == ',' and depth == 0:
                        args.append("".join(curr).strip())
                        curr = []
                    else:
                        if c == '(': depth += 1
                        elif c == ')': depth -= 1
                        curr.append(c)
                if curr:
                    args.append("".join(curr).strip())
                
                # Pre-evaluate arguments recursively (allows nested functions)
                eval_args = []
                for a in args:
                    if '(' in a and ')' in a:
                         # Wrap and recurse
                         eval_args.append(self._evaluate_functions(agent_ref, f"[{a}]", context_ref))
                    else:
                         eval_args.append(a)
                
                args = eval_args
                agent = self.db.get_agent(agent_ref)
                ctx_obj = self.db.get(context_ref or agent_ref)
                
                # --- FUNCTION DISPATCH ---
                if func_name == 'rand':
                    n = int(args[0]) if args[0].isdigit() else 20
                    return str(random.randint(0, n-1)) if n > 0 else "0"
                elif func_name == 'pick':
                    sep = args[1] if len(args) > 1 else '|'
                    items = args[0].split(sep)
                    return random.choice(items).strip() if items else ""
                elif func_name == 'v':
                    attr = args[0]
                    return ctx_obj.attrs.get(attr, "") if ctx_obj else ""
                elif func_name == 'get':
                    target_attr = args[0]
                    if '/' in target_attr:
                        t_ref, t_attr = target_attr.split('/', 1)
                        target = self.match_object(agent_ref, t_ref)
                        if target:
                            return str(target.attrs.get(t_attr, ""))
                    return ""
                
                # Math Functions
                elif func_name == 'add':
                    res = float(args[0]) + float(args[1])
                    return str(int(res)) if res.is_integer() else str(res)
                elif func_name == 'sub':
                    res = float(args[0]) - float(args[1])
                    return str(int(res)) if res.is_integer() else str(res)
                elif func_name == 'mul':
                    res = float(args[0]) * float(args[1])
                    return str(int(res)) if res.is_integer() else str(res)
                elif func_name == 'div':
                    b = float(args[1])
                    if b == 0: return "#DIV/0!"
                    res = float(args[0]) / b
                    return str(int(res)) if res.is_integer() else str(res)
                
                elif func_name == 'math':
                    # Safe evaluation of simple math expressions
                    expr = args[0]
                    # Sanitize: allow numbers and basic operators
                    if re.match(r'^[0-9\.\+\-\*\/\(\)\s]+$', expr):
                        try:
                            # Use eval carefully on sanitized string
                            res = eval(expr, {"__builtins__": None}, {})
                            return str(int(res)) if isinstance(res, (int, float)) and hasattr(res, 'is_integer') and res.is_integer() else str(res)
                        except:
                            return "#MATH_ERR!"
                    return "#NAN!"

                # Date/Time
                elif func_name in ['date', 'time', 'datetime']:
                    if func_name == 'date': return datetime.now().strftime('%A, %B %d, %Y')
                    if func_name == 'time': return datetime.now().strftime('%I:%M %p').lstrip('0')
                    return datetime.now().strftime('%A, %B %d, %Y at %I:%M %p').replace(' 0', ' ')

            except Exception:
                return f"!!{func_name}_err!!"
                
            return f"[{content}]"

        # Safe recursion depth
        for _ in range(5):
            new_text = re.sub(r'\[([^\[\]]+)\]', replacer, text)
            if new_text == text:
                break
            text = new_text
            
        return text

    def _substitute_placeholders(self, agent_ref: str, text: str, trigger_override: str = None) -> str:
        """
        Substitute MUSH-style placeholders and functions in text.
        %n - trigger name, %! - actor name, %l - location name, %# - trigger dbref
        [func(args)] - function evaluation
        """
        if not text: return ""
        
        # Determine actor and trigger
        actor = self.db.get(agent_ref)
        trigger_ref = trigger_override or self._current_trigger or agent_ref
        trigger = self.db.get(trigger_ref)
        
        if not actor: return text
        
        # Basic substitutions
        text = text.replace('%!', actor.name)
        text = text.replace('%n', trigger.name if trigger else "Someone")
        text = text.replace('%#', trigger_ref)
        
        location = self.db.get(actor.location)
        text = text.replace('%l', location.name if location else "Nowhere")
        
        # Evaluate functions
        return self._evaluate_functions(agent_ref, text, trigger_ref)
    
    # ─────────────────────────────────────────────────────────────
    # Basic Commands
    # ─────────────────────────────────────────────────────────────
    
    def _cmd_look(self, agent_ref: str, args: str) -> CommandResult:
        """Look at the current room or a specific object."""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        # If looking at something specific (handle special keywords as plain 'look')
        if args and args.lower().strip() not in ['here', 'room', 'current']:
            return self._look_at_object(agent, args)
        
        loc = self.db.get(agent.location)
        if not loc:
            return CommandResult(False, "You are nowhere.")
        
        # Build description
        lines = []
        header_name = self._evaluate_functions(agent_ref, loc.name, loc.dbref)
        if loc.type != 'room':
            lines.append(f"**{header_name} [Inside]** ({loc.dbref})")
        else:
            lines.append(f"**{header_name}** ({loc.dbref})")
            
            
        # [VR CHECK] Check for Subjective Reality (Per-Player)
        # If this room is VR-enabled AND this player has a VR overlay, use it.
        vr_desc_key = f"_vr_desc_{agent.dbref}"  # Per-player experience
        
        if getattr(loc, 'vr_ok', False):
            if vr_desc_key in loc.attrs:
                # Use the player's existing VR Description
                base_desc = loc.attrs.get(vr_desc_key, loc.desc)
                lines.append(self._evaluate_functions(agent_ref, base_desc, loc.dbref))
                lines.append(f"\n*(VR Active. Type `@reset` to return to base reality.)*")
            elif self.ai:
                # AUTO-EVOLVE: First look in VR room, generate initial reality
                vr_memo = loc.attrs.get("_vr_memo", "")
                vr_intent = loc.attrs.get("_vr_intent", "")
                
                context = {
                    'current_desc': loc.desc,  # Start from base description
                    'trigger': "User enters the VR environment and looks around.",
                    'agent_name': agent.name,
                    'vr_memo': vr_memo,
                    'vr_intent': vr_intent
                }
                
                new_desc = self.ai.evolve_room(context)
                if new_desc:
                    # Capture any embedded [vr_desc] / [vr_title] commands
                    self.capture_robot_intent(agent_ref, new_desc)
                    # Strip commands from visible output
                    clean_desc = re.sub(r'\[vr_desc [^\]]+\]', '', new_desc)
                    clean_desc = re.sub(r'\[vr_title [^\]]+\]', '', clean_desc)
                    clean_desc = re.sub(r'\[scene_change\]', '', clean_desc, flags=re.IGNORECASE).strip()
                    # Store if not already set by [vr_desc]
                    if vr_desc_key not in loc.attrs:
                        loc.attrs[vr_desc_key] = clean_desc
                    lines.append(self._evaluate_functions(agent_ref, clean_desc, loc.dbref))
                    lines.append(f"\n*(VR Active. Type `@reset` to return to base reality.)*")
                else:
                    # Fallback to base if AI fails
                    lines.append(self._evaluate_functions(agent_ref, loc.desc, loc.dbref))
            else:
                # No AI, just show base
                lines.append(self._evaluate_functions(agent_ref, loc.desc, loc.dbref))
        else:
            # Standard Description (not VR)
            lines.append(self._evaluate_functions(agent_ref, loc.desc, loc.dbref))
        
        
        # List other contents
        # Filter Offline Players and Add Metadata
        import time
        now = time.time()
        
        raw_others = [a for a in self.db.get_room_contents(loc.dbref) if a.dbref != agent_ref]
        visible = []
        for o in raw_others:
            if o.type == 'agent' and not getattr(o, 'autonomous', False):
                 last = getattr(o, 'last_interaction', 0)
                 # elapsed = now - last
                 if (now - last) > 300: continue
            visible.append(o)

        if visible:
            lines.append("")
            for other in visible:
                # Flags: P=Player, W=Wizard, R=Robot, S=Search, V=Vehicle
                flags = []
                if other.type == 'agent':
                    if getattr(other, 'wizard', False): flags.append('W')
                    if getattr(other, 'autonomous', False): flags.append('R')
                    else: flags.append('P')
                if getattr(other, 'search_ok', False): flags.append('S')
                if getattr(other, 'enter_ok', False): flags.append('V')
                
                fstr = "".join(flags)
                # Format: "Name (#123 P)"
                meta = f" ({other.dbref}" + (f" {fstr}" if fstr else "") + ")"
                
                eval_name = self._evaluate_functions(agent_ref, other.name, other.dbref)
                if other.type == 'agent':
                    lines.append(f"*{eval_name}*{meta} is here.")
                else:
                    lines.append(f"You see {eval_name}{meta} here.")
        
        # List exits (if it's a room)
        if loc.type == 'room':
            exits = self.db.get_room_exits(loc.dbref)
            if exits:
                exit_names = [e.name for e in exits]
                lines.append("")
                lines.append(f"**Exits:** {', '.join(exit_names)}")
        else:
            lines.append("")
            lines.append("Type 'exit' to leave.")
        
        return CommandResult(
            True, 
            "\n".join(lines),
            context={
                'action': 'look',
                'location': loc.to_dict(),
                'agent': agent.to_dict(),
                'others': [a.to_dict() for a in visible],
                'reaction_instruction': getattr(loc, 'adesc', '') if loc.ai_ok else ""
            }
        )
    
    def _look_at_object(self, agent: GameObject, target: str) -> CommandResult:
        """Look at a specific object with smart matching."""
        # Check room first, then inventory
        obj = self.match_object(agent.dbref, target)
        if not obj:
            # --- VR INTERCEPTION: Look at imaginary things ---
            loc = self.db.get_room(agent.location)
            if loc and getattr(loc, 'vr_ok', False) and self.ai:
                # Let the AI describe this imaginary thing
                vr_desc_key = f"_vr_desc_{agent.dbref}"
                current_desc = loc.attrs.get(vr_desc_key, loc.desc)
                vr_memo = loc.attrs.get("_vr_memo", "")
                vr_intent = loc.attrs.get("_vr_intent", "")
                
                context = {
                    'current_desc': current_desc,
                    'trigger': f"User looks closely at: {target}",
                    'agent_name': agent.name,
                    'vr_memo': vr_memo,
                    'vr_intent': vr_intent
                }
                
                # Use AI to describe what the player sees
                imagined_desc = self.ai.evolve_room(context)
                if imagined_desc:
                    # Capture any embedded [vr_desc] / [vr_title] commands
                    self.capture_robot_intent(agent.dbref, imagined_desc)
                    # Strip commands from visible output
                    clean_desc = re.sub(r'\[vr_desc [^\]]+\]', '', imagined_desc)
                    clean_desc = re.sub(r'\[vr_title [^\]]+\]', '', clean_desc)
                    clean_desc = re.sub(r'\[scene_change\]', '', clean_desc, flags=re.IGNORECASE).strip()
                    # Update their VR state with this new detail (if not already set)
                    if vr_desc_key not in loc.attrs:
                        loc.attrs[vr_desc_key] = clean_desc
                    return CommandResult(True, f"**{target}** *(imagined)*\n\n{clean_desc}\n\n*(VR Active)*")
            
            # Normal failure (not VR)
            msg = f"You don't see '{target}' here."
            if getattr(agent, 'autonomous', False):
                 msg = f"{agent.name} doesn't see '{target}' here."
            return CommandResult(False, msg)
            
        header = f"**{self._evaluate_functions(agent.dbref, obj.name, obj.dbref)}** ({obj.dbref})"
        if obj.type == 'exit':
            dest = self.db.get_room(obj.destination)
            dest_name = dest.name if dest else "somewhere"
            return CommandResult(True, f"{header} → {dest_name}")
            
        desc = obj.desc if obj.desc else "You see nothing special."
        if getattr(agent, 'autonomous', False) and desc == "You see nothing special.":
            desc = f"{agent.name} sees nothing special."

        eval_desc = self._evaluate_functions(agent.dbref, desc, obj.dbref)
        
        ctx = {'action': 'look', 'target': obj.to_dict()}
        if obj.ai_ok:
            ctx['reaction_instruction'] = getattr(obj, 'adesc', '')
            if self.ai:
                ai_hallucination = self.ai.generate_hallucination(self.get_ai_context(agent.dbref, obj.dbref, 'look'))
                eval_desc = f"{eval_desc}\n\n{ai_hallucination}"
            
        return CommandResult(True, f"{header}\n{eval_desc}", context=ctx)

    def _trigger_vehicle_ai(self, vehicle: GameObject, event_text: str, room: GameObject):
        """Trigger AI narration for a vehicle's passengers."""
        if not vehicle.ai_ok or not self.ai:
            return

        # Prepare context for AI
        room_dict = room.to_dict()
        
        # Enrich room context with resolved exits (GameObject.exits is just dbrefs)
        exits_refs = room.exits
        resolved_exits = []
        if exits_refs:
            for ex_ref in exits_refs:
                ex_obj = self.db.get(ex_ref)
                if ex_obj:
                    resolved_exits.append(ex_obj.to_dict())
        room_dict['exits'] = resolved_exits
        
        context = {
            'target': vehicle.to_dict(),
            'instruction': vehicle.attrs.get('INSTRUCTION', vehicle.desc),
            'room_context': room_dict,
            'history': [], # We don't strictly need history for this momentary narration
            'memo': getattr(vehicle, 'memo', ''),
            'conversation_depth': 0
        }
        
        # We phrase the 'last_action' as the event to react to
        reaction = self.ai.get_reactive_action(context, event_text)
        
        # Emit to vehicle interior
        if reaction and reaction != "[idle]":
            # Strip [idle] if it prepends? clean it?
            # get_reactive_action returns clean text + commands?
            # We want to emit it.
            # "Pleasure Yacht says, '...'"? Or just the text?
            # If it includes [say ...], process_command handles it?
            # No, we are emitting.
            
            # The AI layer returns text like: "I see land! [say Land ho!]"
            # We want to execute the commands?
            # If we just emit the text, it shows brackets.
            
            # If we treat it as a command processing result?
            # We can use self.process_command(vehicle.dbref, reaction)?
            # But reaction might be mixed text/command.
            
            # AI Layer usually returns: "Narrative text. [command]"
            # We should probably process it as a command if it has one?
            # And emit the narrative?
            
            # Simplified: Just emit the text for now.
            # Or better: Extract command and execute, emit narrative.
            # But get_reactive_action returns a string.
            
            # Let's just emit it. AI usually formats commands in brackets.
            # If we want ARIA to speak, she should use [say].
            # If we emit the raw string, players see "[say ...]".
            
            # Ideally we parse it. But for now, let's just emit.
            # The internal echo might need cleaning.
            
            # ACTUALLY: MASH AI usually executes the command via the "dumb orchestrator"? 
            # In `_resolve_ai_reaction` (somewhere?), we parse the response.
            # We should do the same here.
             
            # Let's parse the response (simple regex for [command])
            import re
            cmd_match = re.search(r'\[(.*?)\]', reaction)
            narrative = re.sub(r'\[(.*?)\]', '', reaction).strip()
            
            if narrative:
                # Emit narrative to interior (vehicle is the "room" context)
                # Format: "🌐 [Vehicle Name]: Narrative"
                msg = f"🌐\n**{vehicle.name}**: {narrative}"
                # Announce to the vehicle (which acts as a room for passengers)
                self.db.room_announce(vehicle.dbref, msg)
                    
            if cmd_match:
                cmd = cmd_match.group(1)
                # Execute command as vehicle
                self.process_command(vehicle.dbref, cmd)

    def _cmd_look_out(self, agent_ref: str, args: str) -> CommandResult:
        """Look outside the current container."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        # Check if inside something
        container = self.db.get(agent.location)
        if not container or container.type == 'room':
            return CommandResult(False, "You aren't inside anything to look out of.")
            
        outside_loc = self.db.get(container.location)
        if not outside_loc:
            return CommandResult(False, "There's nothing outside.")
            
        # Build description of outside
        lines = []
        header_name = self._evaluate_functions(agent_ref, outside_loc.name, outside_loc.dbref)
        lines.append(f"**{header_name}** ({outside_loc.dbref})")
        lines.append(self._evaluate_functions(agent_ref, outside_loc.desc, outside_loc.dbref))
        
        # List contents
        others = [a for a in self.db.get_room_contents(outside_loc.dbref) if a.dbref != container.dbref]
        if others:
            lines.append("")
            for other in others:
                eval_name = self._evaluate_functions(agent_ref, other.name, other.dbref)
                if other.type == 'agent':
                    lines.append(f"*{eval_name}* is here.")
                else:
                    lines.append(f"You see {eval_name} here.")
                    
        # Exits
        if outside_loc.type == 'room':
            exits = self.db.get_room_exits(outside_loc.dbref)
            if exits:
                exit_names = [e.name for e in exits]
                lines.append("")
                lines.append(f"**Exits:** {', '.join(exit_names)}")
                
        desc_text = "\n".join(lines)
        
        # Trigger AI
        if getattr(container, 'robot', False):
            self._trigger_vehicle_ai(container, f"Passenger inside looks outside at {outside_loc.name}.", outside_loc)
            
        return CommandResult(True, desc_text)

    
    def _cmd_enter(self, agent_ref: str, args: str) -> CommandResult:
        """Enter an object. Syntax: enter <object>"""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        if not args: return CommandResult(False, "Enter what?")
        
        target = self.match_object(agent_ref, args)
        if not target:
            return CommandResult(False, f"I don't see '{args}' here.")
            
        if not getattr(target, 'enter_ok', False):
            return CommandResult(False, f"You can't enter **{target.name}**.")
        
        if target.dbref == agent_ref:
            return CommandResult(False, "You can't enter yourself!")
        
        # If the object is in your inventory, drop it first
        if target.dbref in agent.inventory:
            # Auto-drop the object to current location
            agent.inventory.remove(target.dbref)
            target.location = agent.location
            
        # Check lock
        if not self.passes_lock(agent_ref, target):
            return CommandResult(False, f"**{target.name}** is locked.")

            
        old_loc = agent.location
        old_room = self.db.get(old_loc)
        
        # Departure Announcement
        self._announce_departure(agent_ref, old_loc, f"has entered **{target.name}**.")
        
        self.db.move_agent(agent_ref, target.dbref)
        
        # Arrival Announcement
        self._announce_arrival(agent_ref, target.dbref, f"has entered from **{old_room.name if old_room else 'somewhere'}**.")
        
        # --- INVENTORY SYNC ---
        # Treat entering as being "picked up" by the container
        if agent_ref not in target.inventory:
            target.inventory.append(agent.dbref)
        
        look_result = self._cmd_look(agent_ref, "")
        return CommandResult(
            True, 
            f"You step inside **{target.name}**.\n\n{look_result.message}",
            context={
                'action': 'move',
                'from_room': {'dbref': old_loc, 'name': old_room.name if old_room else "somewhere"},
                'to_room': {'dbref': target.dbref, 'name': target.name},
                'agent': agent.to_dict()
            }
        )
        
    def _cmd_exit(self, agent_ref: str, args: str) -> CommandResult:
        """Exit the current container."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        curr_loc = self.db.get(agent.location)
        if not curr_loc or curr_loc.type == 'room':
            return CommandResult(False, "You aren't inside anything you can leave.")
            
        # Move to the location of the container
        dest_ref = curr_loc.location
        if not dest_ref:
            return CommandResult(False, "This container is nowhere!")
            
        # Departure Announcement
        self._announce_departure(agent_ref, agent.location, f"has stepped out of **{curr_loc.name}**.")
        
        self.db.move_agent(agent_ref, dest_ref)
        
        # Arrival Announcement
        self._announce_arrival(agent_ref, dest_ref, f"has stepped out of **{curr_loc.name}**.")
        
        # --- INVENTORY SYNC ---
        # Treat exiting as being "dropped" from the container's inventory
        if agent_ref in curr_loc.inventory:
            curr_loc.inventory.remove(agent_ref)
        
        look_result = self._cmd_look(agent_ref, "")
        return CommandResult(
            True, 
            f"You step out of **{curr_loc.name}**.\n\n{look_result.message}",
            context={
                'action': 'move',
                'from_room': {'dbref': agent.location, 'name': curr_loc.name},
                'to_room': {'dbref': dest_ref, 'name': (self.db.get(dest_ref).name if self.db.get(dest_ref) else "somewhere")},
                'agent': agent.to_dict()
            }
        )

    def _cmd_get(self, agent_ref: str, args: str) -> CommandResult:
        """Pick up an object. Syntax: get <object>"""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        if not args: return CommandResult(False, "Get what?")
        
        target = self.match_object(agent_ref, args)
        if not target:
            return CommandResult(False, f"I don't see '{args}' here.")
            
        if target.type != 'object':
            return CommandResult(False, "You can't pick that up.")
            
        if target.dbref == agent_ref:
            return CommandResult(False, "You can't pick yourself up!")
            
        # Ownership check or lock check
        if not self.passes_lock(agent_ref, target):
            return CommandResult(False, f"**{target.name}** is too heavy or locked.")
            
        # Move to inventory
        agent.inventory.append(target.dbref)
        # Update target location
        target.location = agent_ref
        if agent.location in self.db._location_index:
            if target.dbref in self.db._location_index[agent.location]:
                self.db._location_index[agent.location].remove(target.dbref)
        
        # Add to agent's location index (for contents tracking)
        if target.location not in self.db._location_index:
            self.db._location_index[target.location] = []
        self.db._location_index[target.location].append(target.dbref)
        
        return CommandResult(
            True, 
            f"You pick up **{target.name}**.", 
            message_3p=f"**{agent.name}** picks up **{target.name}**.",
            context={'action': 'get', 'object': target.to_dict()}
        )

    def _cmd_drop(self, agent_ref: str, args: str) -> CommandResult:
        """Drop an object. Syntax: drop <object>"""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        if not args: return CommandResult(False, "Drop what?")
        
        # Find in inventory
        target = None
        for ref in agent.inventory:
            obj = self.db.get(ref)
            if obj and obj.name.lower().startswith(args.lower()):
                target = obj
                break
                
        if not target:
            return CommandResult(False, f"You aren't carrying '{args}'.")
            
        # Move from inventory to room
        agent.inventory.remove(target.dbref)
        
        # Remove from agent's location index
        if target.location in self.db._location_index:
            if target.dbref in self.db._location_index[target.location]:
                self.db._location_index[target.location].remove(target.dbref)
                
        target.location = agent.location
        
        # Update indices
        if target.location not in self.db._location_index:
            self.db._location_index[target.location] = []
        self.db._location_index[target.location].append(target.dbref)
        
        return CommandResult(
            True, 
            f"You drop **{target.name}**.", 
            message_3p=f"**{agent.name}** drops **{target.name}**.",
            context={'action': 'drop', 'object': target.to_dict()}
        )
    
    # ─────────────────────────────────────────────────────────────
    # Ejection Logic (for vehicles dropping passengers)
    # ─────────────────────────────────────────────────────────────
    # Note: _cmd_drop naturally handles passengers if they are in inventory.
    # The logic above just resets location to agent.location (container's room).
    # That works perfectly for ejection.


    def _cmd_set_enter_ok(self, agent_ref: str, args: str) -> CommandResult:
        """Toggle enter_ok flag. Syntax: @enter_ok <target>=<yes|no>"""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        if '=' not in args:
            return CommandResult(False, "Usage: `@enter_ok <target>=<yes|no>`")
            
        target_str, val = args.split('=', 1)
        target = self.match_object(agent_ref, target_str.strip())
        if not target:
            return CommandResult(False, f"I don't see '{target_str.strip()}' here.")
            
        if not self.can_modify(agent_ref, target):
            return CommandResult(False, f"You don't own **{target.name}**.")
            
        target.enter_ok = val.strip().lower() in ['yes', 'true', '1', 'on']
        status = "ENABLED" if target.enter_ok else "DISABLED"
        return CommandResult(True, f"Enter-ability for **{target.name}** is now {status}.")

    def _cmd_set_vr_ok(self, agent_ref: str, args: str) -> CommandResult:
        """Toggle VR Mode for a room."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        if '=' not in args:
             return CommandResult(False, "Usage: `@vr_ok <room>=<yes|no>`")
             
        target_str, val = args.split('=', 1)
        target = self.match_object(agent_ref, target_str.strip())
        
        if not target:
             return CommandResult(False, f"I don't see '{target_str.strip()}' here.")
             
        if not self.can_modify(agent_ref, target):
             return CommandResult(False, "You can't modify that.")
             
        # Set Flag
        state = val.strip().lower() in ['yes', 'true', '1', 'on']
        target.vr_ok = state
        
        status = "ENABLED" if state else "DISABLED"
        return CommandResult(True, f"VR Improvisational Mode for **{target.name}** is now {status}.")


    def _cmd_go(self, agent_ref: str, args: str) -> CommandResult:
        """Move through an exit."""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        if not args:
            return CommandResult(False, "Go where?")
        
        if args.startswith('#'):
            exit_obj = self.db.get(args)
            if exit_obj and exit_obj.type != 'exit': exit_obj = None
        else:
            exit_obj = self.db.find_exit_by_name(agent.location, args)
        
        # --- DRIVING LOGIC (Proxy Move) ---
        if not exit_obj:
            # If not found locally, check if we are in a vehicle that can take this exit
            loc = self.db.get(agent.location)
            if loc and loc.type != 'room':
                 container = loc
                 # Check if container's location has the exit
                 container_exit = self.db.find_exit_by_name(container.location, args)
                 if container_exit:
                     # Check if we can drive it (simple check: are we inside?)
                     # Execute move ON BEHALF of the vehicle
                     return self._cmd_go(container.dbref, args)

        if not exit_obj:
            return CommandResult(False, f"You can't go '{args}' from here.")
        
        # Check lock
        if not self.passes_lock(agent_ref, exit_obj):
            return CommandResult(False, f"The **{exit_obj.name}** exit is locked.")
        
        # Move the agent
        old_room = self.db.get_room(agent.location)

        # [VR CLEANUP]
        # If the OLD room was a VR room, we should clear the subjective state
        # when the user leaves.
        if old_room and getattr(old_room, 'vr_ok', False):
             vr_key = f"_vr_{agent.dbref}"
             if vr_key in old_room.attrs:
                 del old_room.attrs[vr_key]



        new_room = self.db.get_room(exit_obj.destination)
        
        if not new_room:
            return CommandResult(False, "That exit leads nowhere!")
        
        # Departure Announcement
        departure_msg = f"has left for **{new_room.name}** (via {exit_obj.name})."
        self._announce_departure(agent_ref, old_room.dbref, departure_msg)
        
        # TRIGGER DEPARTURE TICKS (Old Room)
        # We must trigger this BEFORE moving, or at least pass the old room ref.
        # But we need the announcement to be in history first.
        # _announce_departure adds to history.
        # Now we manually trigger reactions in the OLD room.
        self.trigger_room_reactions(old_room.dbref, agent_ref, f"PRESENCE_DEPARTURE: {agent.name} {departure_msg}")

        self.db.move_agent(agent_ref, new_room.dbref)
    
        # Arrival Announcement
        arrival_msg = f"has arrived from **{old_room.name}**."
        self._announce_arrival(agent_ref, new_room.dbref, arrival_msg)
        
        # TRIGGER ARRIVAL TICKS (New Room)
        self.trigger_room_reactions(new_room.dbref, agent_ref, f"PRESENCE_ARRIVAL: {agent.name} {arrival_msg}")
        
        # Auto-look at the new room
        look_res = self._cmd_look(agent_ref, "")
        
        # Maybe find tokens!
        token_msg = self.maybe_drop_tokens(agent_ref)
        
        # Return combined message: movement + look + tokens
        final_msg = f"You go to **{new_room.name}**.\n\n{look_res.message}"
        if token_msg:
            final_msg += f"\n\n{token_msg}"
            
        # Vehicle Arrival Narration (if acting as vehicle)
        if getattr(agent, 'enter_ok', False):
             # 1. Trigger AI Narration
             if getattr(agent, 'robot', False):
                self._trigger_vehicle_ai(agent, f"Arrived at {new_room.name}.", new_room)
             
             # 2. Auto-Gaze: Announce outside view to passengers
             passengers = [p for p in self.db.get_room_contents(agent.dbref) if p.type == 'agent']
             if passengers:
                 # Use the first passenger to generate the view for everyone
                 # This ensures passengers see where they have arrived immediately
                 lo_res = self._cmd_look_out(passengers[0].dbref, "")
                 if lo_res.success:
                     self.db.room_announce(agent.dbref, f"\n{lo_res.message}")
            
        return CommandResult(
            True,
            final_msg,
            message_3p=f"**{agent.name}** goes to **{new_room.name}**.",
            context=look_res.context
        )
    
    def _cmd_say(self, agent_ref: str, args: str) -> CommandResult:
        """Say something to the room."""
        agent = self.db.get(agent_ref)
        if not agent or not agent.location:
            return CommandResult(False, "You don't exist or have no location!")
        
        if not args:
            return CommandResult(False, "Say what?")
            
        text = self._substitute_placeholders(agent_ref, args)
        
        self.db.room_announce(agent.location, f"🌐\n**{agent.name}** says, \"{text}\"", exclude=agent_ref)
        
        # Trigger Listen Patterns
        self._trigger_listen_patterns(agent_ref, text)
        
        return CommandResult(
            True,
            f'You say, "{text}"',
            context={'action': 'say', 'agent': agent.to_dict(), 'text': text}
        )
    
    def _cmd_pose(self, agent_ref: str, args: str) -> CommandResult:
        """Pose an action."""
        agent = self.db.get(agent_ref)
        if not agent or not agent.location:
            return CommandResult(False, "You don't exist or have no location!")
        
        if not args:
            return CommandResult(False, "Pose what?")
            
        text = self._substitute_placeholders(agent_ref, args)
        
        self.db.room_announce(agent.location, f"🌐\n**{agent.name}** {text}", exclude=agent_ref)
        
        # Trigger Listen Patterns (Poses count as 'hearing' an action description)
        self._trigger_listen_patterns(agent_ref, f"{agent.name} {text}")
        
        return CommandResult(
            True,
            f'{agent.name} {text}',
            context={'action': 'pose', 'agent': agent.to_dict(), 'text': text}
        )
        
    def _cmd_emit(self, agent_ref: str, args: str) -> CommandResult:
        """Emit raw text to the room."""
        agent = self.db.get(agent_ref)
        if not agent or not agent.location:
            return CommandResult(False, "You don't exist or have no location!")
        
        if not args:
            return CommandResult(False, "Emit what?")
            
        text = self._substitute_placeholders(agent_ref, args)
        
        # Announce to others in the room
        self.db.room_announce(agent.location, f"🌐\n{text}", exclude=agent_ref)
        
        # Trigger Listen Patterns
        self._trigger_listen_patterns(agent_ref, text)
        
        # Return the text to the emitter so THEY see it too
        return CommandResult(
            True,
            text,
            context={'action': 'emit', 'agent': agent.to_dict(), 'text': text}
        )

    
    def _cmd_inventory(self, agent_ref: str, args: str) -> CommandResult:
        """List inventory and token balance."""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        lines = []
        
        # Token balance
        tokens = self.get_tokens(agent_ref)
        if tokens == float('inf'):
            lines.append("**Tokens:** ∞ (Wizard)")
        else:
            lines.append(f"**Tokens:** {tokens}")
        
        lines.append("")
        
        # Inventory
        if agent.inventory:
            lines.append("**Carrying:**")
            for item_ref in agent.inventory:
                item = self.db.get(item_ref)
                if item:
                    lines.append(f"  • {item.name}")
        else:
            lines.append("**Carrying:** nothing")
        
        return CommandResult(True, "\n".join(lines))
    
    def _cmd_give_item(self, agent_ref: str, args: str) -> CommandResult:
        """
        Give an item to another agent.
        Usage: give <object> to <player>
        """
        if " to " not in args:
             return CommandResult(False, "Usage: give <object> to <player>")
             
        target_name, recipient_name = args.split(" to ", 1)
        recipient_name = recipient_name.strip()
        
        # 1. Find the Item in INVENTORY
        agent = self.db.get_agent(agent_ref)
        item = None
        for obj_ref in agent.inventory:
            obj = self.db.get(obj_ref)
            if obj and (target_name.lower() in obj.name.lower() or obj.dbref == target_name):
                item = obj
                break
        
        if not item:
            return CommandResult(False, f"You don't have '{target_name}'.")
            
        # 2. Find the Recipient (Must be in room)
        recipient = self.match_object(agent_ref, recipient_name)
        if not recipient or recipient.location != agent.location:
             return CommandResult(False, f"I don't see '{recipient_name}' here.")
             
        if recipient.type != 'agent':
             return CommandResult(False, f"You can't give things to {recipient.name}.")
             
        # 3. Transfer
        agent.inventory.remove(item.dbref)
        recipient.inventory.append(item.dbref)
        item.owner = recipient.dbref 
        
        # Update Location & Index (Critical for lookups)
        if item.location in self.db._location_index:
            if item.dbref in self.db._location_index[item.location]:
                self.db._location_index[item.location].remove(item.dbref)
        
        item.location = recipient.dbref
        
        if recipient.dbref not in self.db._location_index:
            self.db._location_index[recipient.dbref] = []
        self.db._location_index[recipient.dbref].append(item.dbref)

        self.db.save(WORLD_FILE)
        
        return CommandResult(True, f"You gave {item.name} to {recipient.name}.", 
                             message_3p=f"{agent.name} gave {item.name} to {recipient.name}.")



    def _cmd_exits(self, agent_ref: str, args: str) -> CommandResult:
        """List exits from current room."""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        exits = self.db.get_room_exits(agent.location)
        if not exits:
            return CommandResult(True, "There are no obvious exits.")
        
        lines = ["**Exits:**"]
        for e in exits:
            dest = self.db.get_room(e.destination)
            dest_name = dest.name if dest else "???"
            lines.append(f"  • {e.name} → {dest_name} ({e.dbref})")
        
        return CommandResult(True, "\n".join(lines))
    
    def _cmd_help(self, agent_ref: str, args: str) -> CommandResult:
        """Show help - auto-generated from command registry."""
        # If specific command requested
        if args:
            cmd_name = args.lower().strip()
            
            # Check commands
            if cmd_name in self.command_meta:
                meta = self.command_meta[cmd_name]
                lines = [f"**{meta['name']}**"]
                lines.append(f"Usage: `{meta['usage']}`")
                if meta['aliases']:
                    lines.append(f"Aliases: {', '.join(meta['aliases'])}")
                lines.append(f"\n{meta['help']}")
                return CommandResult(True, "\n".join(lines))
                
            # Check functions
            if cmd_name in self.function_meta:
                meta = self.function_meta[cmd_name]
                lines = [f"**{meta['name']} Function**"]
                lines.append(f"Usage: `{meta['usage']}`")
                lines.append(f"\n{meta['help']}")
                return CommandResult(True, "\n".join(lines))
                
            # Check placeholders (like help %n)
            if cmd_name in self.placeholder_meta:
                lines = [f"**Placeholder: {cmd_name}**"]
                lines.append(f"\n{self.placeholder_meta[cmd_name]}")
                return CommandResult(True, "\n".join(lines))
                
            # Check for special topics
            if cmd_name == 'softcode':
                return CommandResult(True, self._get_softcode_help())
            
            if cmd_name == 'topics':
                return CommandResult(True, self._get_topics_help())
            
            # Check for category-specific help
            category_map = {
                'movement': 'Movement',
                'senses': 'Senses',
                'communication': 'Communication',
                'economy': 'Economy',
                'building': 'Building',
                'ownership': 'Ownership',
                'functions': 'Functions',
                'placeholders': 'Placeholders',
                'system': 'System',
                'admin': 'Admin'
            }
            if cmd_name in category_map:
                return CommandResult(True, self._get_category_help(category_map[cmd_name]))
                
            return CommandResult(False, f"No help found for: {cmd_name}")
        
        # Build categorized help
        categories = {}
        category_order = ['Movement', 'Senses', 'Communication', 'Economy', 'Building', 'Ownership', 'Functions', 'Placeholders', 'System', 'Other']
        
        for name, meta in self.command_meta.items():
            cat = meta['category']
            if cat not in categories:
                categories[cat] = []
            
            # Format: `usage` (aliases) — help
            alias_str = f" ({', '.join(meta['aliases'])})" if meta['aliases'] else ""
            categories[cat].append(f"  `{meta['usage']}`{alias_str} — {meta['help']}")
            
        # Add Functions
        categories['Functions'] = []
        for name, meta in self.function_meta.items():
            categories['Functions'].append(f"  `{meta['usage']}` — {meta['help']}")
            
        # Add Placeholders
        categories['Placeholders'] = []
        for code, help_text in self.placeholder_meta.items():
            categories['Placeholders'].append(f"  `{code}` — {help_text}")
        
        # Build output
        lines = ["**MASH Commands:**\n"]
        
        for cat in category_order:
            if cat in categories:
                lines.append(f"**{cat}:**")
                lines.extend(sorted(categories[cat]))
                lines.append("")
        
        # Add note about wizard commands from app.py
        lines.append("**Admin (from @who, @dump, @reload):**")
        lines.append("  See sidebar help for admin commands.")
        lines.append("\n**Tip:** Use `help softcode` for a guide on programmable triggers, or `help topics` to see all help categories.")
        
        return CommandResult(True, "\n".join(lines))

    def _get_softcode_help(self) -> str:
        """Detailed guide for the Softcode system."""
        return """
### 🛠️ MASH Softcode Guide
Softcode allows you to create interactive objects using attributes.

**1. The Listening Flag**
Objects must be set to listening to process triggers: 
`@listening <object>=yes`

**2. Dollar Commands ($)**
Custom commands that trigger when someone types a specific phrase.
Format: `&ATTR <obj>=$pattern:action`
- Example (Greeter): `&GREET Statue=$hi:say Hello, %n!`
- Example (Magic 8-Ball): `&SHAKE Ball=$shake:emit The ball says: [pick(Yes!|No.|Perhaps...)]`

**3. Listen Patterns (^)**
Ambient triggers that react to speech or emotes in the room.
Format: `&ATTR <obj>=^pattern:action`
- Example (Watcher): `&WATCH Eyes=^waves:emit The eyes follow %n's movement.`
- Example (Parrot): `&ECHO Parrot=^*:say %0!`

**4. Wildcards & Placeholders**
- `%0`: The full text matched by the pattern.
- `%n`: Name of the player who triggered it.
- `%!`: Name of the object performing the action.
- `%#`: DBRef of the trigger.
- `%l`: Location Name.

**5. Functions**
Inject logic into actions using `[function()]` syntax.
- `rand(n)`: Random number from 0 to n-1.
- `pick(list)`: Pick a random item from a `|` delimited list.
- `v(attr)`: Get value of an attribute on the object itself.
- `get(obj/attr)`: Get attribute value from another object.

**6. Scripting Blocks ({ })**
You can paste multiple commands or execute complex scripts by wrapping them in Curly Braces `{ }`. Inside a block, commands can be separated by newlines or semicolons `;`.
- Example: 
```
{
  @create Orb; @describe Orb=A glowing orb.
  &ROLL Orb=$roll:emit The orb pulses: [rand(100)]
  look Orb
}
```
"""
    
    def _get_topics_help(self) -> str:
        """List all help topics with highlighted names."""
        topics = [
            ('movement', 'Movement commands (go, enter, exit, get, drop, home)'),
            ('senses', 'Sensory commands (look, smell, taste, touch, listen)'),
            ('communication', 'Speech and emotes (say, pose, emit)'),
            ('economy', 'Token system (give, tokens, inventory)'),
            ('building', 'Creating and configuring objects (@create, @dig, @set, @ai_ok, @search_ok)'),
            ('ownership', 'Permissions and locks (@lock, @chown, examine)'),
            ('functions', 'Inline functions for softcode ([rand], [pick], [v], [get])'),
            ('placeholders', 'Dynamic substitution codes (%n, %!, %l, %#)'),
            ('system', 'System commands (@who, @deep_research, help)'),
            ('softcode', 'Guide to programmable triggers ($ and ^ patterns)'),
        ]
        
        lines = ["### 📚 Help Topics\n"]
        lines.append("Use `help <topic>` to learn more about a specific area.\n")
        
        for topic, desc in topics:
            lines.append(f"  `{topic}` — {desc}")
        
        lines.append("\n**Example:** `help building` or `help softcode`")
        return "\n".join(lines)
    
    def _get_category_help(self, category: str) -> str:
        """Get help for a specific category of commands."""
        lines = [f"### 📖 {category} Commands\n"]
        
        # Collect commands in this category
        cmds = []
        for name, meta in self.command_meta.items():
            if meta['category'] == category:
                alias_str = f" ({', '.join(meta['aliases'])})" if meta['aliases'] else ""
                cmds.append(f"  `{meta['usage']}`{alias_str} — {meta['help']}")
        
        # Special handling for Functions and Placeholders
        if category == 'Functions':
            for name, meta in self.function_meta.items():
                cmds.append(f"  `{meta['usage']}` — {meta['help']}")
        
        if category == 'Placeholders':
            for code, help_text in self.placeholder_meta.items():
                cmds.append(f"  `{code}` — {help_text}")
        
        if category == 'Admin':
            cmds = [
                "  `@who` — List all connected players",
                "  `@dump` — Save world state to disk",
                "  `@reload` — Reload world from disk (discards unsaved changes)",
                "  `@boot <player>` — Disconnect a player (wizard only)",
            ]
        
        if cmds:
            lines.extend(sorted(cmds))
        else:
            lines.append("  No commands found in this category.")
        
        lines.append(f"\n**Tip:** Use `help topics` to see all categories.")
        return "\n".join(lines)
    
    # ─────────────────────────────────────────────────────────────
    # Sensory Commands
    # ─────────────────────────────────────────────────────────────
    
    def _resolve_sense_target(self, agent: GameObject, target: str):
        """Resolve a target for sensory commands using smart matching."""
        obj = self.match_object(agent.dbref, target)
        if obj:
            return (obj, None)
            
        if not target or target.lower() in ['here', 'room']:
            return (self.db.get(agent.location), None) # Return the room object
            
        return (None, f"You don't see '{target}' here.")
    
    def _cmd_smell(self, agent_ref: str, args: str) -> CommandResult:
        """Smell the room or a target."""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        target, err = self._resolve_sense_target(agent, args)
        if err:
            return CommandResult(False, err)
        
        scent = getattr(target, 'olfactory', '') or None
        if scent:
            text = scent
        else:
            # Placeholder for AI generation
            text = "You don't notice any particular scent."
            
        eval_text = self._evaluate_functions(agent_ref, text, target.dbref)
        
        ctx = {'action': 'smell', 'target': target.to_dict()}
        if target.ai_ok:
            ctx['reaction_instruction'] = getattr(target, 'asmell', '')
            if self.ai:
                ai_text = self.ai.generate_hallucination(self.get_ai_context(agent_ref, target.dbref, 'smell'))
                eval_text = f"{eval_text}\n\n{ai_text}"
            
        return CommandResult(True, f"**Smell:** {eval_text}", context=ctx)
    
    def _cmd_taste(self, agent_ref: str, args: str) -> CommandResult:
        """Taste something."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        if not args: return CommandResult(False, "Taste what?")
        
        target, err = self._resolve_sense_target(agent, args)
        if err: return CommandResult(False, err)
        
        text = getattr(target, 'flavor', '') or "It doesn't have much of a taste."
        eval_text = self._evaluate_functions(agent_ref, text, target.dbref)
        
        ctx = {'action': 'taste', 'target': target.to_dict()}
        if target.ai_ok:
            ctx['reaction_instruction'] = getattr(target, 'ataste', '')
            if self.ai:
                ai_text = self.ai.generate_hallucination(self.get_ai_context(agent_ref, target.dbref, 'taste'))
                eval_text = f"{eval_text}\n\n{ai_text}"
            
        return CommandResult(True, f"**Taste:** {eval_text}", context=ctx)
    
    def _cmd_touch(self, agent_ref: str, args: str) -> CommandResult:
        """Touch something."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        if not args: return CommandResult(False, "Touch what?")
        
        target, err = self._resolve_sense_target(agent, args)
        if err: return CommandResult(False, err)
        
        text = getattr(target, 'tactile', '') or "It feels ordinary to the touch."
        eval_text = self._evaluate_functions(agent_ref, text, target.dbref)
        
        ctx = {'action': 'touch', 'target': target.to_dict()}
        if target.ai_ok:
            ctx['reaction_instruction'] = getattr(target, 'atouch', '')
            if self.ai:
                ai_text = self.ai.generate_hallucination(self.get_ai_context(agent_ref, target.dbref, 'touch'))
                eval_text = f"{eval_text}\n\n{ai_text}"
            
        return CommandResult(True, f"**Touch:** {eval_text}", context=ctx)
    
    def _cmd_listen(self, agent_ref: str, args: str) -> CommandResult:
        """Listen to surroundings or something."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        target, err = self._resolve_sense_target(agent, args)
        if err: return CommandResult(False, err)
        
        text = getattr(target, 'auditory', '') or "You don't hear anything unusual."
        eval_text = self._evaluate_functions(agent_ref, text, target.dbref)
        
        ctx = {'action': 'listen', 'target': target.to_dict()}
        if target.ai_ok:
            ctx['reaction_instruction'] = getattr(target, 'alisten', '')
            if self.ai:
                ai_text = self.ai.generate_hallucination(self.get_ai_context(agent_ref, target.dbref, 'listen'))
                eval_text = f"{eval_text}\n\n{ai_text}"
            
        return CommandResult(True, f"**Listen:** {eval_text}", context=ctx)
    
    # Sensory attribute setters
    
    def _cmd_set_smell(self, agent_ref: str, args: str) -> CommandResult:
        """Set smell attribute. Syntax: @smell <target>=<description>"""
        return self._set_sensory_attr(agent_ref, args, 'olfactory', 'smell')
    
    def _cmd_set_taste(self, agent_ref: str, args: str) -> CommandResult:
        """Set taste attribute. Syntax: @taste <target>=<description>"""
        return self._set_sensory_attr(agent_ref, args, 'flavor', 'taste')
    
    def _cmd_set_touch(self, agent_ref: str, args: str) -> CommandResult:
        """Set touch attribute. Syntax: @touch <target>=<description>"""
        return self._set_sensory_attr(agent_ref, args, 'tactile', 'texture')
    
    def _cmd_set_listen(self, agent_ref: str, args: str) -> CommandResult:
        """Set listen attribute. Syntax: @listen <target>=<description>"""
        return self._set_sensory_attr(agent_ref, args, 'auditory', 'sound')
    
    def _set_sensory_attr(self, agent_ref: str, args: str, attr_name: str, friendly_name: str) -> CommandResult:
        """Generic setter for sensory attributes."""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        if '=' not in args:
            return CommandResult(False, f"Usage: `@{friendly_name} <me|here|target>=<description>`")
        
        target_str, value = args.split('=', 1)
        target_str = target_str.strip()
        value = value.strip()
        
        target, err = self._resolve_sense_target(agent, target_str)
        if err:
            return CommandResult(False, err)
        
        # Check ownership
        if not self.can_modify(agent_ref, target):
            return CommandResult(False, f"You don't own **{target.name}**.")
        
        # Convert escape sequences to actual characters
        value = value.replace('\\n', '\n').replace('\\t', '\t')
        
        setattr(target, attr_name, value)
        return CommandResult(True, f"Set {friendly_name} for **{target.name}**: {value}")


    # ─────────────────────────────────────────────────────────────
    # Deep Research Logic
    # ─────────────────────────────────────────────────────────────
    
    def _cmd_deep_research(self, agent_ref: str, args: str) -> CommandResult:
        """Start a deep research background job."""
        if not self.ai:
            return CommandResult(False, "AI Engine not connected.")
            
        if not args:
            return CommandResult(False, "Usage: `@deep_research <topic>`")
            
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        # Check Tokens (Free for Robots/AI)
        COST = 100
        if agent.robot:
            COST = 0
            
        if agent.tokens < COST:
            return CommandResult(False, f"Insufficient funds. Deep Research costs **{COST} Tokens** (You have {agent.tokens}).")
            
        # Check Lock
        if self.research_lock.locked() or (self.current_research_job and self.current_research_job['status'] == 'RUNNING'):
            return CommandResult(False, "⚠️ A Deep Research job is already in progress. Please wait for it to complete.")
            
        # Deduct Funds
        agent.tokens -= COST
        
        # Initialize Job
        topic = args.strip()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_topic = "".join([c if c.isalnum() else "_" for c in topic])[:20]
        filename = f"Research_{safe_topic}_{timestamp}.md"
        
        # Use configured research path
        save_dir = self.research_path
        if not os.path.exists(save_dir) and save_dir:
            os.makedirs(save_dir, exist_ok=True)
            
        save_path = os.path.join(save_dir, filename)
        
        self.current_research_job = {
            'actor': agent.name,
            'topic': topic,
            'status': 'RUNNING',
            'start_time': datetime.now(),
            'output_path': save_path
        }
        
        # Spawn Thread
        t = threading.Thread(target=self._research_worker, args=(agent.to_dict(), topic, save_path))
        t.start()
        
        return CommandResult(True, f"🧪 **{agent.name}** begins Deep Research on: *{topic}*.\n(Cost: {COST} Tokens. Notification on completion.)")

    def _research_worker(self, agent_dict: Dict, topic: str, save_path: str):
        """Background worker for research."""
        with self.research_lock:
            try:
                # Context for AI
                context = {'actor': agent_dict}
                result = self.ai.perform_deep_research(context, topic, save_path)
                
                self.current_research_job['status'] = 'COMPLETED'
                self.current_research_job['result'] = result
                
                # Optional: Announce to room? No, that requires thread-safe DB access which is risky.
                # We'll let the user poll or see the toast.
                
            except Exception as e:
                self.current_research_job['status'] = 'FAILED'
                self.current_research_job['error'] = str(e)

    # Snapshot Logic
    # ─────────────────────────────────────────────────────────────

    def _cmd_snapshot(self, agent_ref: str, args: str) -> CommandResult:
        """Capture a high-fidelity image of the current scene (Async)."""
        if not self.ai:
            return CommandResult(False, "AI Engine not connected.")
            
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")

        # Check Tokens (Wizard only free?)
        COST = 50
        if agent.wizard: COST = 0 # Wizards get free snapshots
            
        if agent.tokens < COST:
            return CommandResult(False, f"Insufficient funds. Snapshot costs **{COST} Tokens** (You have {agent.tokens}).")

        # Check Lock
        if self.snapshot_lock.locked() or (self.current_snapshot_job and self.current_snapshot_job['status'] == 'RUNNING'):
            return CommandResult(False, "⚠️ A Snapshot is already being synthesized. Please wait.")

        # Deduct Funds
        agent.tokens -= COST

        # Capture Context immediately (before moving/changing)
        ctx = self.get_scene_context(agent_ref)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"Snapshot_{timestamp}.png"
        
        save_dir = self.snapshot_path
        if not os.path.exists(save_dir) and save_dir:
            os.makedirs(save_dir, exist_ok=True)
            
        save_path = os.path.join(save_dir, filename)

        self.current_snapshot_job = {
            'actor': agent.name,
            'status': 'RUNNING',
            'start_time': datetime.now(),
            'output_path': save_path,
            'context': ctx
        }

        # Spawn Thread
        t = threading.Thread(target=self._snapshot_worker, args=(save_path,))
        t.start()

        return CommandResult(True, f"🎨 **{agent.name}** triggers a Visual Loom snapshot.\n(Cost: {COST} Tokens. Synthesis processing in background...)")

    def _snapshot_worker(self, save_path: str):
        """Background worker for snapshots."""
        with self.snapshot_lock:
            try:
                ctx = self.current_snapshot_job.get('context')
                # 1. Bloom the prompt
                prompt = self.ai.get_image_prompt(ctx)
                # 2. Generate the image
                img_data = self.ai.generate_image(prompt)
                
                if img_data:
                    with open(save_path, "wb") as f:
                        f.write(img_data)
                    self.current_snapshot_job['status'] = 'COMPLETED'
                else:
                    self.current_snapshot_job['status'] = 'FAILED'
                    self.current_snapshot_job['error'] = "AI generated no image data."
                
            except Exception as e:
                self.current_snapshot_job['status'] = 'FAILED'
                self.current_snapshot_job['error'] = str(e)

    # ─────────────────────────────────────────────────────────────
    # Building Commands
    # ─────────────────────────────────────────────────────────────
    
    def _cmd_set(self, agent_ref: str, args: str) -> CommandResult:
        """Set a custom attribute. Syntax: @set <target>/<attr>=<value>"""
        if '=' not in args or '/' not in args.split('=')[0]:
            return CommandResult(False, "Usage: `@set <target>/<attr>=<value>`")
            
        target_part, value = args.split('=', 1)
        target_str, attr_name = target_part.split('/', 1)
        
        return self._perform_set(agent_ref, target_str.strip(), attr_name.strip(), value.strip())

    def _cmd_set_shortcut(self, agent_ref: str, args: str) -> CommandResult:
        """MUSH-style attribute set: &ATTR target=value"""
        if ' ' not in args or '=' not in args:
            return CommandResult(False, "Usage: `&ATTR <target>=<value>`")
            
        attr_name, rest = args.split(None, 1)
        if '=' not in rest:
            return CommandResult(False, "Usage: `&ATTR <target>=<value>`")
            
        target_str, value = rest.split('=', 1)
        return self._perform_set(agent_ref, target_str.strip(), attr_name.strip(), value.strip())

    def _perform_set(self, agent_ref: str, target_str: str, attr_name: str, value: str) -> CommandResult:
        """Internal helper to set an attribute after parsing."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        target = self.match_object(agent_ref, target_str)
        if not target:
            return CommandResult(False, f"I don't see '{target_str}' here.")
            
        if not self.can_modify(agent_ref, target):
            return CommandResult(False, f"You don't own **{target.name}**.")
            
        if not value:
            if attr_name in target.attrs:
                del target.attrs[attr_name]
                return CommandResult(True, f"Cleared attribute **{attr_name}** from **{target.name}**.")
            return CommandResult(False, f"Attribute **{attr_name}** not found.")
        
        # Convert escape sequences to actual characters
        value = value.replace('\\n', '\n').replace('\\t', '\t')
        
        target.attrs[attr_name.strip()] = value
        return CommandResult(True, f"Set **{attr_name}** on **{target.name}** to: {value}")

        
    def _cmd_set_adesc(self, agent_ref: str, args: str) -> CommandResult:
        return self._set_ai_reaction(agent_ref, args, 'adesc', 'AI look reaction')
        
    def _cmd_set_asmell(self, agent_ref: str, args: str) -> CommandResult:
        return self._set_ai_reaction(agent_ref, args, 'asmell', 'AI smell reaction')
        
    def _cmd_set_ataste(self, agent_ref: str, args: str) -> CommandResult:
        return self._set_ai_reaction(agent_ref, args, 'ataste', 'AI taste reaction')
        
    def _cmd_set_atouch(self, agent_ref: str, args: str) -> CommandResult:
        return self._set_ai_reaction(agent_ref, args, 'atouch', 'AI touch reaction')
        
    def _cmd_set_alisten(self, agent_ref: str, args: str) -> CommandResult:
        return self._set_ai_reaction(agent_ref, args, 'alisten', 'AI listen reaction')
        
    def _cmd_set_listening(self, agent_ref: str, args: str) -> CommandResult:
        """Toggle listening flag. Syntax: @listening <target>=<yes|no>"""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        if '=' not in args: return CommandResult(False, "Usage: `@listening <target>=<yes|no>`")
        
        target_str, val = args.split('=', 1)
        target = self.match_object(agent_ref, target_str.strip())
        if not target: return CommandResult(False, f"I don't see '{target_str.strip()}' here.")
        
        if not self.can_modify(agent_ref, target):
            return CommandResult(False, "Permission denied.")
            
        target.listening = val.strip().lower() in ['yes', 'true', '1', 'on']
        status = "LISTENING" if target.listening else "NOT listening"
        return CommandResult(True, f"**{target.name}** is now {status}.")

    def _cmd_set_vehicle(self, agent_ref: str, args: str) -> CommandResult:
        """Set vehicle type. Syntax: @vehicle <target>=<type>"""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        if '=' not in args: return CommandResult(False, "Usage: `@vehicle <target>=<type>` (e.g., boat, aircraft, mech)")
        
        target_str, val = args.split('=', 1)
        target = self.match_object(agent_ref, target_str.strip())
        if not target: return CommandResult(False, f"I don't see '{target_str.strip()}' here.")
        
        if not self.can_modify(agent_ref, target):
            return CommandResult(False, "Permission denied.")
        
        vehicle_type = val.strip().lower()
        if not vehicle_type:
            # Clear the vehicle type
            target.vehicle_type = ''
            return CommandResult(True, f"**{target.name}** is no longer a vehicle.")
        
        target.vehicle_type = vehicle_type
        return CommandResult(True, f"**{target.name}** is now a **{vehicle_type}** vehicle. Use `@lock <exit>=vehicle:{vehicle_type}` to restrict exits.")

    def _cmd_set_search_ok(self, agent_ref: str, args: str) -> CommandResult:
        """Set the search_ok flag. Usage: @search_ok <target>=<true/false>"""
        return self._set_bool_field(agent_ref, args, 'search_ok', "Set AI Search Grounding")

    def _set_bool_field(self, agent_ref: str, args: str, field: str, action_name: str) -> CommandResult:
        """Internal helper for setting boolean flags."""
        if '=' not in args:
             return CommandResult(False, f"Usage: `<command> <target>=<true/false>`")
        
        target_str, value_str = args.split('=', 1)
        val = value_str.lower().strip() in ['true', 'yes', '1', 'on']
        
        target = self.match_object(agent_ref, target_str.strip())
        if not target:
            return CommandResult(False, f"I don't see '{target_str.strip()}' here.")
            
        if not self.can_modify(agent_ref, target):
            return CommandResult(False, f"You don't own **{target.name}**.")
            
        setattr(target, field, val)
        status = "ENABLED" if val else "DISABLED"
        return CommandResult(True, f"{action_name} for **{target.name}**: **{status}**")

    def _set_ai_reaction(self, agent_ref: str, args: str, attr: str, friendly_name: str) -> CommandResult:
        """Internal helper to set AI reaction instructions (@a-attributes)."""
        if '=' not in args:
            return CommandResult(False, f"Usage: `@{attr} <target>=<instructions>`")
            
        target_str, instructions = args.split('=', 1)
        target = self.match_object(agent_ref, target_str.strip())
        if not target:
            return CommandResult(False, f"I don't see '{target_str.strip()}' here.")
            
        if not self.can_modify(agent_ref, target):
            return CommandResult(False, f"You don't own **{target.name}**.")
            
        setattr(target, attr, instructions.strip())
        return CommandResult(True, f"Set {friendly_name} for **{target.name}**.")

    def _add_to_history(self, agent_ref: str, command: str, response: str):
        """Log a command and its response for AI context."""
        agent = self.db.get(agent_ref)
        location = agent.location if agent else None
        
        self.history.append({
            'agent': agent_ref,
            'location': location,
            'command': command,
            'response': response
        })
        if len(self.history) > self.max_history:
            self.history.pop(0)

    def get_history(self, limit: int = 5, location_ref: str = None) -> List[Dict[str, str]]:
        """Get recent command/response history for AI context."""
        if location_ref:
            filtered = [h for h in self.history if h['location'] == location_ref]
            return filtered[-limit:]
        return self.history[-limit:]
        
    def get_ai_context(self, agent_ref: str, target_ref: str, action: str) -> Dict[str, Any]:
        """
        Package context for a generative AI call.
        Includes history (local to room), instruction, actor info, and target state.
        """
        agent = self.db.get(agent_ref)
        target = self.db.get(target_ref)
        
        if not agent or not target:
            return {}
            
        # Determine instruction attribute based on action (or use target name if robot)
        if getattr(target, 'robot', False):
            instruction = target.desc # Robots use their desc as personality prompt
        else:
            instr_attr = {
                'look': 'adesc',
                'smell': 'asmell',
                'taste': 'ataste',
                'touch': 'atouch',
                'listen': 'alisten'
            }.get(action, 'adesc')
            instruction = getattr(target, instr_attr, '')
        
        room_ctx = {}
        if agent.location:
             room_obj = self.db.get(agent.location)
             if room_obj:
                 room_ctx = room_obj.to_dict()
                 # Manually inject exits for AI validation (One-Hop Peek)
                 exits = self.db.get_room_exits(agent.location)
                 exits_data = []
                 for e in exits:
                     e_dict = e.to_dict()
                     # Resolve Destination Name
                     if e.destination:
                         dest_obj = self.db.get(e.destination)
                         if dest_obj:
                             e_dict['destination_name'] = dest_obj.name
                     exits_data.append(e_dict)
                 
                 room_ctx['exits'] = exits_data
                 
                 # Detect enterable objects
                 contents = self.db.get_room_contents(agent.location)
                 room_ctx['enterable_objects'] = [o.to_dict() for o in contents if getattr(o, 'enter_ok', False) and o.dbref != agent_ref]
                 
                 # Detect if we can exit (inside a container)
                 room_ctx['can_exit'] = (room_obj.type != 'room')

        return {
            'action': action,
            'actor': agent.to_dict(),
            'target': target.to_dict(),
            'instruction': instruction,
            'memo': getattr(target, 'memo', ''),
            'status': getattr(target, 'status', ''),
            'history': self.get_history(10, location_ref=agent.location),
            'room_context': room_ctx
        }
        
    def capture_robot_intent(self, robot_ref: str, ai_output: str) -> List[CommandResult]:
        """
        Parse AI output for embedded commands.
        Example: "I look at the door. [go north]" -> executes 'go north' as robot.
        """
        # First, ensure the "robot" is actually an agent capable of acting.
        # Objects like the Magic 8-Ball are ai_ok but not agents.
        robot = self.db.get_agent(robot_ref)
        if not robot:
            return []

        results = []
        # Find all content between square brackets (non-recursive for simple commands)
        commands = re.findall(r'\[([^\[\]]+)\]', ai_output)
        for cmd in commands:
            cmd_text = cmd.strip()
            
            # Special case: [memo <text>] - OVERWRITE MEMORY
            if cmd_text.lower().startswith("memo "):
                memo_content = cmd_text[5:].strip()
                if len(memo_content) > 5000:
                    memo_content = memo_content[:5000]
                target = self.db.get(robot_ref)
                if target:
                    target.memo = memo_content
                results.append(CommandResult(True, "", context={'action': 'memo'}))
                continue

            # Special case: [remember <text>] - APPEND MEMORY
            if cmd_text.lower().startswith("remember "):
                new_fact = cmd_text[9:].strip()
                target = self.db.get(robot_ref)
                if target:
                    current_memo = target.memo or ""
                    # Check for overflow (5KB limit)
                    if len(current_memo) + len(new_fact) + 5 > 5000:
                        # FAIL & PROMPT UPSUM
                        msg = f"SYSTEM ALERT: Memory Full ({len(current_memo)}/5000). You tried to remember '{new_fact[:20]}...'. You MUST perform a [memo <summary>] now to consolidate."
                        results.append(CommandResult(True, msg, context={'action': 'system_alert', 'alert': msg}))
                    else:
                        # Append with bullet
                        if current_memo:
                            target.memo = f"{current_memo}\n- {new_fact}"
                        else:
                            target.memo = f"- {new_fact}"
                        results.append(CommandResult(True, "", context={'action': 'remember'}))
                continue

            # Special case: [status <text>] - OVERWRITE INTENT
            if cmd_text.lower().startswith("status ") or cmd_text.lower().startswith("upsum "):
                prefix_len = 7 if cmd_text.lower().startswith("status ") else 6
                status_content = cmd_text[prefix_len:].strip()
                if len(status_content) > 2000:
                    status_content = status_content[:2000]
                target = self.db.get(robot_ref)
                if target:
                    target.status = status_content
                results.append(CommandResult(True, "", context={'action': 'status'}))
                continue

            # Special case: [goal <text>] - APPEND INTENT
            if cmd_text.lower().startswith("goal "):
                new_goal = cmd_text[5:].strip()
                target = self.db.get(robot_ref)
                if target:
                    current_status = target.status or ""
                    # Check for overflow (2KB limit)
                    if len(current_status) + len(new_goal) + 5 > 2000:
                         # FAIL & PROMPT UPSUM
                        msg = f"SYSTEM ALERT: Intent Full ({len(current_status)}/2000). You tried to add goal '{new_goal[:20]}...'. You MUST perform a [status <mission>] now to consolidate."
                        results.append(CommandResult(True, msg, context={'action': 'system_alert', 'alert': msg}))
                    else:
                         # Append with semicolon
                        if current_status:
                            target.status = f"{current_status}; {new_goal}"
                        else:
                            target.status = new_goal
                        results.append(CommandResult(True, "", context={'action': 'goal'}))
                continue

            # Special case: [vr_desc <player>=<description>] - SET PLAYER VR DESCRIPTION
            if cmd_text.lower().startswith("vr_desc "):
                vr_content = cmd_text[8:].strip()
                if '=' in vr_content:
                    parts = vr_content.split('=', 1)
                    player_name = parts[0].strip()
                    vr_description = parts[1].strip()
                    
                    # Find the player
                    player = self.match_object(robot_ref, player_name)
                    if player and player.type == 'agent':
                        # Get the player's current location (must be VR room)
                        loc = self.db.get_room(player.location)
                        if loc and getattr(loc, 'vr_ok', False):
                            vr_desc_key = f"_vr_desc_{player.dbref}"
                            loc.attrs[vr_desc_key] = vr_description
                            results.append(CommandResult(True, "", context={'action': 'vr_desc', 'player': player.name}))
                        else:
                            results.append(CommandResult(False, "Player not in VR room.", context={'action': 'vr_desc_fail'}))
                    else:
                        results.append(CommandResult(False, f"Player '{player_name}' not found.", context={'action': 'vr_desc_fail'}))
                else:
                    results.append(CommandResult(False, "Usage: [vr_desc player=description]", context={'action': 'vr_desc_fail'}))
                continue

            # Special case: [vr_title <player>=<title>] - SET PLAYER VR SCENE TITLE
            if cmd_text.lower().startswith("vr_title "):
                vr_content = cmd_text[9:].strip()
                if '=' in vr_content:
                    parts = vr_content.split('=', 1)
                    player_name = parts[0].strip()
                    vr_title = parts[1].strip()
                    
                    # Find the player
                    player = self.match_object(robot_ref, player_name)
                    if player and player.type == 'agent':
                        # Get the player's current location (must be VR room)
                        loc = self.db.get_room(player.location)
                        if loc and getattr(loc, 'vr_ok', False):
                            vr_title_key = f"_vr_title_{player.dbref}"
                            loc.attrs[vr_title_key] = vr_title
                            results.append(CommandResult(True, "", context={'action': 'vr_title', 'player': player.name}))
                        else:
                            results.append(CommandResult(False, "Player not in VR room.", context={'action': 'vr_title_fail'}))
                    else:
                        results.append(CommandResult(False, f"Player '{player_name}' not found.", context={'action': 'vr_title_fail'}))
                else:
                    results.append(CommandResult(False, "Usage: [vr_title player=title]", context={'action': 'vr_title_fail'}))
                continue

            res = self.process_command(robot_ref, cmd_text)
            
            # SUPPRESSION LOGIC (Telepresence Glitch Fix):
            # If a robot executes a command (success OR failure),
            # we do NOT want the "You go..." or "You can't..." feedback to bubble up.
            # We rely on the Room Announce messages instead.
            low_cmd = cmd_text.lower()
            
            # Check for movement/speech commands (Success or Fail)
            is_action_cmd = (
                low_cmd.startswith("go ") or 
                low_cmd.startswith("enter ") or 
                low_cmd.startswith("exit") or 
                low_cmd.startswith("say ") or 
                low_cmd.startswith("virtual ")
            )
            
            # SIDE-EFFECT CAPTURE:
            # We want to show what the robot did (Third Person), not what they saw (First Person).
            
            # 1. Check for Explicit Context (Say/Pose/Go from built-in commands)
            if res.success and res.context:
                action_type = res.context.get('action')
                if action_type == 'say':
                     text = res.context.get('text', '')
                     # Robot speech is handled by narrative and room_announce; silence the return
                     results.append(CommandResult(True, "", context={'action': 'say'}))
                elif action_type == 'pose':
                     text = res.context.get('text', '')
                     # Robot pose is handled by narrative and room_announce; silence the return
                     results.append(CommandResult(True, "", context={'action': 'pose'}))
            
            # 2. Check for Implicit Side Effects (Departure/Arrival)
            elif res.success:
                matches = self.get_history(3, location_ref=None) 
                if matches:
                    # Iterate backwards
                    for h in reversed(matches):
                        # If this history event belongs to the robot and is an announcement...
                        if h['agent'] == robot_ref and h.get('command') in ["PRESENCE_DEPARTURE", "PRESENCE_ARRIVAL"]:
                             # This is the public text we want to show!
                             # Only add it if we haven't already added a similar message
                             if not any(r.message == h['response'] for r in results):
                                 results.append(CommandResult(True, h['response']))
                             break # Found the relevant event, stop scanning
            
            if is_action_cmd:
                 # Suppress the private feedback ("You go..." / "You say...")
                 pass
            elif res.success and "You go" in res.message:
                 # Catch-all for successful movement (if context missing)
                 pass
            else:
                 # Standard logic for other commands (look, inv, etc)
                 if not res.success and "You can't" in res.message:
                     pass
                 else:
                     results.append(res)
                 
        return results

    def trigger_room_reactions(self, room_ref: str, actor_ref: str, last_action: str) -> List[Dict[str, Any]]:
        """
        Scan a room for robots/ai_ok objects and ask them to react to a player action.
        Returns list of results: {name, narrative, message}
        """
        if not self.ai:
            return []
            
        actor = self.db.get_agent(actor_ref)
        is_player = (actor_ref == "#1") # Simple check for now, or check autonomous=False
        
        # Initialize or Update Conversation Counters
        if not hasattr(self, 'conversation_counters'):
            self.conversation_counters = {} # {room_ref: turns_remaining}
        
        if is_player:
            # Dynamic Conversation Depth based on context
            # Default: Short interaction (1-2 turns)
            depth = 1
            
            # Simple heuristic: Count mentioned agents or keywords
            mentioned = []
            lower_action = last_action.lower()
            
            # Check for group keywords
            if any(w in lower_action for w in ['everyone', 'all', 'both', 'guys', 'team']):
                import random
                depth = random.randint(4, 6) # Long conversation
            else:
                # Check specific agent names in room
                agents_in_room = [a.name.lower() for a in self.db.get_room_contents(room_ref) if a.type == 'agent']
                count = sum(1 for name in agents_in_room if name in lower_action)
                
                if count > 1:
                    import random
                    depth = random.randint(3, 5) # Medium group
                elif count == 1:
                    depth = 2 # Reply + Expecting one follow-up
                else:
                    depth = 1 # Just a reaction
            
            self.conversation_counters[room_ref] = depth
        elif actor and actor.robot:
            # Robot spoke: Burn a turn
            turns = self.conversation_counters.get(room_ref, 0)
            if turns > 0:
                self.conversation_counters[room_ref] = turns - 1
            else:
                # Conversation exhausted, stop triggering
                return []
        
        turns_remaining = self.conversation_counters.get(room_ref, 0)
        
        smart_objs = [obj for obj in self.db.get_room_contents(room_ref) 
                     if (obj.ai_ok or obj.robot) and obj.dbref != actor_ref]
        
        results = []
        for obj in smart_objs:
            # Special logic for Robots: If everyone left, they might want to follow or go home
            is_empty = len([a for a in self.db.get_room_contents(room_ref) if a.type == 'agent' and not a.robot]) == 0
            
            # Get AI context for this object's reaction
            ctx = self.get_ai_context(actor_ref, obj.dbref, 'reaction')
            
            # Inject Conversation Urgency into Context for AI Prompt
            ctx['conversation_depth'] = turns_remaining
            
            # Ask the AI for a response to the specific 'last_action'
            # We explicitly mention if the player is now GONE from the room
            event_prefix = ""
            if "has left" in last_action or "PRESENCE_DEPARTURE" in last_action:
                event_prefix = "[PLAYER LEAVING] "
            
            # --- BRANCH: Robot (Agency) OR Object (Atmosphere) ---
            if obj.robot:
                search_mode = None
                if getattr(obj, 'search_ok', False): search_mode = 'grounding'
                
                # 1. Robots get full agency (Narrative + Commands)
                ai_output = self.ai.get_reactive_action(ctx, f"{event_prefix}{last_action}", search_mode=search_mode)
                
                # If AI returned [idle], skip
                if '[idle]' in ai_output.lower():
                    # If room is empty and robot is alone, they might decide to go home autonomously
                    if is_empty and obj.home and obj.location != obj.home:
                        self.process_command(obj.dbref, "home")
                        results.append({
                            'name': obj.name,
                            'narrative': "retires to their home quarters.",
                            'intent_messages': [f"✨ **{obj.name}** has gone home."]
                        })
                    continue
                    
                # Extract narrative and commands
                clean_narrative = re.sub(r'\[.*?\]', '', ai_output).strip()
                if clean_narrative:
                    self._add_to_history(obj.dbref, "AI_REACTION", f"{obj.name}: {clean_narrative}")
                
                # Robots can execute commands
                intents = self.capture_robot_intent(obj.dbref, ai_output)
                intent_msgs = [r.message for r in intents if r.message]
                
                results.append({
                    'name': obj.name,
                    'dbref': obj.dbref,
                    'narrative': clean_narrative,
                    'intent_messages': intent_msgs
                })
            else:
                # 2. Passive Objects (Atmosphere Only, No Commands)
                # Check for search grounding on objects (e.g., Magic 8-Ball as Oracle)
                obj_search_mode = None
                if getattr(obj, 'search_ok', False): obj_search_mode = 'grounding'
                
                atmosphere = self.ai.get_atmospheric_flavor(ctx, f"{event_prefix}{last_action}", search_mode=obj_search_mode)
                
                if atmosphere and '[idle]' not in atmosphere.lower():
                    clean_flavor = re.sub(r'\[.*?\]', '', atmosphere).strip()
                    if clean_flavor:
                        self._add_to_history(obj.dbref, "AI_FLAVOR", f"{obj.name} (Atmosphere): {clean_flavor}")
                        results.append({
                            'name': obj.name,
                            'dbref': obj.dbref,
                            'narrative': clean_flavor,
                            'intent_messages': []
                        })
            
        return results

    def _cmd_set_ai_ok(self, agent_ref: str, args: str) -> CommandResult:
        """Toggle ai_ok flag. Syntax: @ai_ok <target>=<yes|no>"""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        if '=' not in args: return CommandResult(False, "Usage: `@ai_ok <target>=<yes|no>`")
            
        target_str, val = args.split('=', 1)
        target = self.match_object(agent_ref, target_str.strip())
        if not target: return CommandResult(False, f"I don't see '{target_str.strip()}' here.")
        if not self.can_modify(agent_ref, target): return CommandResult(False, f"You don't own **{target.name}**.")
            
        target.ai_ok = val.strip().lower() in ['yes', 'true', '1', 'on']
        status = "ENABLED" if target.ai_ok else "DISABLED"
        return CommandResult(True, f"AI reactions for **{target.name}** are now {status}.")

    def _cmd_set_robot(self, agent_ref: str, args: str) -> CommandResult:
        """Toggle robot flag. Syntax: @robot <agent>=<yes|no>"""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        if '=' not in args: return CommandResult(False, "Usage: `@robot <agent>=<yes|no>`")
            
        target_str, val = args.split('=', 1)
        target = self.match_object(agent_ref, target_str.strip())
        if not target: return CommandResult(False, f"I don't see '{target_str.strip()}' here.")
        if not self.can_modify(agent_ref, target): return CommandResult(False, f"You don't own **{target.name}**.")
        if target.type != 'agent': return CommandResult(False, "Only agents can be robots.")
            
        target.robot = val.strip().lower() in ['yes', 'true', '1', 'on']
        # Robot implies ai_ok
        if target.robot: target.ai_ok = True
        
        status = "ENABLED" if target.robot else "DISABLED"
        return CommandResult(True, f"Robot mode for **{target.name}** is now {status}.")


    def _cmd_dig(self, agent_ref: str, args: str) -> CommandResult:
        """Create a new room. Syntax: @dig <room name>"""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        if not args:
            return CommandResult(False, "Usage: `@dig <room name>`")
        
        # Check tokens
        tokens = self.get_tokens(agent_ref)
        if tokens < COST_DIG:
            return CommandResult(False, f"You need {COST_DIG} tokens to dig a room. You have {tokens}.")
        
        # Spend tokens
        if not self.spend_tokens(agent_ref, COST_DIG):
            return CommandResult(False, "Transaction failed!")
        
        # Create the room
        room = self.db.create_object(
            'room',
            args.strip(),
            desc="An empty room awaiting description.",
            owner=agent_ref
        )
        
        new_balance = self.get_tokens(agent_ref)
        balance_msg = "" if new_balance == float('inf') else f" (Balance: {new_balance})"
        
        return CommandResult(
            True,
            f"You dig **{room.name}** ({room.dbref}).{balance_msg}\n\nUse `@link <exit name>={room.dbref}` to connect it.",
            message_3p=f"**{agent.name}** digs a new room: **{room.name}** ({room.dbref}).",
            context={'action': 'dig', 'room': room.to_dict()}
        )
    
    def _cmd_create(self, agent_ref: str, args: str) -> CommandResult:
        """Create a new object. Syntax: @create <object name>"""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        if not args:
            return CommandResult(False, "Usage: `@create <object name>`")
        
        # Check tokens
        tokens = self.get_tokens(agent_ref)
        if tokens < COST_CREATE:
            return CommandResult(False, f"You need {COST_CREATE} token to create an object. You have {tokens}.")
        
        # Spend tokens
        if not self.spend_tokens(agent_ref, COST_CREATE):
            return CommandResult(False, "Transaction failed!")
        
        # Create the object
        obj = self.db.create_object(
            'object',
            args.strip(),
            desc="", # Empty by default to support custom logic
            owner=agent_ref,
            location=agent_ref  # Goes into inventory
        )
        
        # Add to inventory
        agent.inventory.append(obj.dbref)
        
        new_balance = self.get_tokens(agent_ref)
        balance_msg = "" if new_balance == float('inf') else f" (Balance: {new_balance})"
        
        return CommandResult(
            True,
            f"You create **{obj.name}** ({obj.dbref}).{balance_msg}",
            context={'action': 'create', 'object': obj.to_dict()}
        )

    def _cmd_create_agent(self, agent_ref: str, args: str) -> CommandResult:
        """Create a new autonomous NPC agent. Syntax: @agent <name>"""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        if not args:
            return CommandResult(False, "Usage: `@agent <name>`")
        
        # Check tokens
        tokens = self.get_tokens(agent_ref)
        if tokens < COST_AGENT:
            return CommandResult(False, f"You need {COST_AGENT} tokens to create an agent. You have {tokens}.")
        
        # Spend tokens
        if not self.spend_tokens(agent_ref, COST_AGENT):
            return CommandResult(False, "Transaction failed!")
        
        # Create the NPC agent in the current room
        npc = self.db.create_object(
            'agent',
            args.strip(),
            desc="A newly created NPC.",
            owner=agent_ref,
            location=agent.location,  # Spawns in current room
            autonomous=True,          # It's an NPC, not a player
            robot=False               # Not AI-controlled by default
        )
        
        new_balance = self.get_tokens(agent_ref)
        balance_msg = "" if new_balance == float('inf') else f" (Balance: {new_balance})"
        
        return CommandResult(
            True,
            f"You spawn **{npc.name}** ({npc.dbref}).{balance_msg}\n\nUse `@robot {npc.name}=yes` to enable AI control, `@enter_ok {npc.name}=yes` for vehicles.",
            context={'action': 'create_agent', 'agent': npc.to_dict()}
        )

    
    def _cmd_link(self, agent_ref: str, args: str) -> CommandResult:
        """Create/link an exit. Syntax: @link <exit name>=<destination dbref>"""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        if '=' not in args:
            return CommandResult(False, "Usage: `@link <exit name>=<room dbref>`\nExample: `@link north=#123`")
        
        exit_name, dest_ref = args.split('=', 1)
        exit_name = exit_name.strip()
        dest_ref = dest_ref.strip()
        
        if not exit_name or not dest_ref:
            return CommandResult(False, "Usage: `@link <exit name>=<room dbref>`")
        
        # Validate destination
        dest_room = self.db.get_room(dest_ref)
        if not dest_room:
            return CommandResult(False, f"Destination {dest_ref} is not a valid room.")
        
        current_room = self.db.get_room(agent.location)
        if not current_room:
            return CommandResult(False, "You're not in a room!")
        
        # Check permissions for current room (to create exit)
        if not self.can_modify(agent_ref, current_room):
            return CommandResult(False, f"You don't have permission to build in **{current_room.name}**.")
        
        # Check if exit already exists
        existing = self.db.find_exit_by_name(agent.location, exit_name)
        if existing:
            # Check ownership of the exit
            if not self.can_modify(agent_ref, existing):
                return CommandResult(False, f"You don't own the existing exit **{exit_name}**.")
            
            # Re-link existing exit (free)
            existing.destination = dest_ref
            return CommandResult(
                True,
                f"Re-linked **{exit_name}** → **{dest_room.name}** ({dest_ref})",
                context={'action': 'link', 'exit': existing.to_dict()}
            )
        
        # Check tokens for new exit
        tokens = self.get_tokens(agent_ref)
        if tokens < COST_LINK:
            return CommandResult(False, f"You need {COST_LINK} tokens to create an exit. You have {tokens}.")
        
        # Spend tokens
        if not self.spend_tokens(agent_ref, COST_LINK):
            return CommandResult(False, "Transaction failed!")
        
        # Create new exit

        exit_obj = self.db.create_object(
            'exit',
            exit_name,
            source=agent.location,
            destination=dest_ref,
            owner=agent_ref
        )
        
        # Add exit to current room's exit list
        current_room.exits.append(exit_obj.dbref)
        
        return CommandResult(
            True,
            f"Created exit **{exit_name}** ({exit_obj.dbref}) → **{dest_room.name}** ({dest_ref})",
            context={'action': 'link', 'exit': exit_obj.to_dict()}
        )
    
    def _cmd_describe(self, agent_ref: str, args: str) -> CommandResult:
        """Set description. Syntax: @describe <target>=<description>"""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        if '=' not in args:
            return CommandResult(False, "Usage: `@describe <target>=<description>`")
        
        target_str, desc = args.split('=', 1)
        target_str = target_str.strip()
        desc = desc.strip()
        
        target = self.match_object(agent_ref, target_str)
        if not target:
            return CommandResult(False, f"I don't see '{target_str}' here.")
            
        # Check ownership
        if not self.can_modify(agent_ref, target):
            return CommandResult(False, f"You don't own **{target.name}**.")
        
        # Convert escape sequences to actual characters
        desc = desc.replace('\\n', '\n').replace('\\t', '\t')
        
        target.desc = desc

        return CommandResult(
            True, 
            f"Description set for **{target.name}** ({target.dbref})."
        )
    
    def _cmd_name(self, agent_ref: str, args: str) -> CommandResult:
        """Rename object. Syntax: @name <target>=<new name>"""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        if '=' not in args:
            return CommandResult(False, "Usage: `@name <target>=<new name>`")
        
        target_str, new_name = args.split('=', 1)
        target_str = target_str.strip()
        new_name = new_name.strip()
        
        if not new_name:
            return CommandResult(False, "You must provide a new name.")
            
        target = self.match_object(agent_ref, target_str)
        if not target:
            return CommandResult(False, f"I don't see '{target_str}' here.")
            
        # Check ownership
        if not self.can_modify(agent_ref, target):
            return CommandResult(False, f"You don't own **{target.name}**.")
            
        old_name = target.name
        target.name = new_name
        
        # Re-index if name changed
        if old_name.lower() != new_name.lower():
            self.db.rebuild_indices()
            
        return CommandResult(
            True, 
            f"Renamed **{old_name}** → **{new_name}** ({target.dbref})."
        )

    def _cmd_status(self, agent_ref: str, args: str) -> CommandResult:
        """Set current narrative goal/intent (UPSUM). Supports target=text syntax."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        # Parse target=text syntax
        target = agent
        text = args
        
        if '=' in args:
            parts = args.split('=', 1)
            target_name = parts[0].strip()
            text = parts[1].strip()
            
            # Find target
            found = self.find_target(agent_ref, target_name)
            if not found:
                return CommandResult(False, f"I don't see '{target_name}' here.")
            target = found
            
            # Permission check
            if not self.can_modify(agent_ref, target):
                return CommandResult(False, f"You don't own **{target.name}**.")
        
        if not text: return CommandResult(False, "Usage: `@status [target=]<text>`")
        
        target.status = text[:2000] # 2KB limit
        return CommandResult(True, f"🎯 Intent updated for **{target.name}**.", context={'category': 'System'})

    def _cmd_memo(self, agent_ref: str, args: str) -> CommandResult:
        """Set persistent facts/preferences. Supports target=text syntax."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        # Parse target=text syntax
        target = agent
        text = args
        
        if '=' in args:
            parts = args.split('=', 1)
            target_name = parts[0].strip()
            text = parts[1].strip()
            
            # Find target
            found = self.find_target(agent_ref, target_name)
            if not found:
                return CommandResult(False, f"I don't see '{target_name}' here.")
            target = found
            
            # Permission check
            if not self.can_modify(agent_ref, target):
                return CommandResult(False, f"You don't own **{target.name}**.")
        
        if not text: return CommandResult(False, "Usage: `@memo [target=]<text>`")
        
        target.memo = text[:5000] # 5KB limit
        return CommandResult(True, f"📝 Memory updated for **{target.name}**.", context={'category': 'System'})

    def _cmd_wipe_memory(self, agent_ref: str, args: str) -> CommandResult:
        """Wipe all memory and intent from a robot you own."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        if not args: return CommandResult(False, "Usage: `@wipe <target>`")
        
        target = self.find_target(agent_ref, args.strip())
        if not target:
            return CommandResult(False, f"I don't see '{args}' here.")
        
        if target.type != 'agent':
            return CommandResult(False, f"**{target.name}** is not an agent.")
        
        # Permission check
        if not self.can_modify(agent_ref, target):
            return CommandResult(False, f"You don't own **{target.name}**.")
        
        # Wipe memory and status
        target.memo = ""
        target.status = ""
        
        return CommandResult(True, f"🧹 Memory and intent wiped for **{target.name}**. Tabula rasa.")

    def _cmd_mind_read(self, agent_ref: str, args: str) -> CommandResult:
        """Silent Divinity: Read the persistent facts and current intent of an agent."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        if not agent.wizard: return CommandResult(False, "Permission denied.")
        
        if not args: return CommandResult(False, "Usage: `@mind <target>`")
        
        target = self.match_object(agent_ref, args.strip())
        if not target:
            return CommandResult(False, f"I don't see '{args.strip()}' here.")
            
        if target.type != 'agent':
            return CommandResult(False, f"**{target.name}** has no mind to read.")
            
        memo = getattr(target, 'memo', '')
        if not memo: memo = "No persistent facts."
        
        status = getattr(target, 'status', '')
        if not status: status = "No current intent."
        
        report = (
            f"🧠 **Mind Reading: {target.name}**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 **Persistent Facts (MEMO):**\n{memo}\n\n"
            f"🎯 **Current Intent (UPSUM):**\n{status}"
        )
        return CommandResult(True, report, context={'category': 'System'})

    def _cmd_purge_buffers(self, agent_ref: str, args: str) -> CommandResult:
        """Wizard only: Clear all agent message buffers."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        if not agent.wizard: return CommandResult(False, "Permission denied.")
        
        count = 0
        for obj in self.db.objects.values():
            if obj.type == 'agent' and hasattr(obj, 'message_buffer') and obj.message_buffer:
                obj.message_buffer = []
                count += 1
        
        return CommandResult(True, f"Purged message buffers for {count} agents. Ghost threads severed.", context={'category': 'System'})

    def _cmd_destroy(self, agent_ref: str, args: str) -> CommandResult:
        """Permanently delete an object and get a token refund."""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        if not args:
            return CommandResult(False, "Usage: `@destroy <target>`")
            
        target = self.match_object(agent_ref, args.strip())
        if not target:
            return CommandResult(False, f"I don't see '{args.strip()}' here.")
            
        if target.dbref == "#0" or target.dbref == "#1":
            return CommandResult(False, "You cannot destroy the foundation of the world!")
            
        if not self.can_modify(agent_ref, target):
            return CommandResult(False, f"You don't own **{target.name}**.")
            
        # Determine refund based on type
        if target.type == 'room':
            refund = COST_DIG
        elif target.type == 'agent':
            refund = COST_AGENT
        elif target.type == 'exit':
            refund = COST_LINK
        else:
            refund = COST_CREATE
        owner_ref = target.owner

        
        target_name = target.name
        target_dbref = target.dbref
        target_type = target.type
        
        # Room contents evacuation logic
        if target_type == 'room':
            contents = self.db.get_room_contents(target_dbref)
            for item in contents:
                owner = self.db.get(item.owner)
                if owner:
                    dest = item.owner
                    self.db.move_agent(item.dbref, dest)
                    # Add to inventory if it's an agent owner
                    if item.dbref not in owner.inventory:
                        owner.inventory.append(item.dbref)
                    self.db.room_announce(dest, f"🌐\n{item.name} is returned to your inventory from the collapsing remains of {target_name}.")
                else:
                    # Fallback to Limbo if owner is missing
                    self.db.move_agent(item.dbref, "#0")
                    self.db.room_announce("#0", f"🌐\n{item.name} arrives from the collapsing remains of {target_name}.")
                
        # Perform destruction
        if self.db.destroy_object(target_dbref):
            # Grant refund
            self.add_tokens(owner_ref, refund)
            
            new_balance = self.get_tokens(agent_ref)
            balance_msg = f" (Refunded {refund} Tokens. Balance: {new_balance})"
            
            return CommandResult(
                True, 
                f"Destroyed **{target_name}** ({target_dbref}).{balance_msg}",
                message_3p=f"**{agent.name}** destroyed **{target_name}** ({target_dbref})."
            )
        else:
            return CommandResult(False, "Destruction failed!")
    
    # ─────────────────────────────────────────────────────────────
    # Economy Commands
    # ─────────────────────────────────────────────────────────────
    
    def _cmd_tokens(self, agent_ref: str, args: str) -> CommandResult:
        """Check token balance."""
        tokens = self.get_tokens(agent_ref)
        if tokens == float('inf'):
            return CommandResult(True, "**Tokens:** ∞ (Wizard)")
        return CommandResult(True, f"**Tokens:** {tokens}")
    
    def _cmd_give(self, agent_ref: str, args: str) -> CommandResult:
        """Give tokens or items. Syntax: @give <target>=<amount|item>"""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        if '=' not in args:
            return CommandResult(False, "Usage: `@give <target>=<amount|item>`")
        
        target_name, gift_str = args.split('=', 1)
        target_name = target_name.strip()
        gift_str = gift_str.strip()
        
        # 1. Resolve Target (Must be in same room)
        target = None
        room_contents = self.db.get_room_contents(agent.location)
        for obj in room_contents:
            if obj.type == 'agent' and (obj.name.lower() == target_name.lower() or obj.dbref == target_name):
                target = obj
                break
                
        if not target:
            return CommandResult(False, f"I don't see '{target_name}' here.")
        
        if target.dbref == agent_ref:
            return CommandResult(False, "You can't give things to yourself.")

        # 2. Determine Gift Type (Token vs Item)
        try:
            amount = int(gift_str)
            is_token = True
        except ValueError:
            is_token = False
            
        if is_token:
            # TOKEN LOGIC
            if amount <= 0: return CommandResult(False, "Amount must be positive.")
            my_tokens = self.get_tokens(agent_ref)
            if my_tokens != float('inf') and my_tokens < amount:
                return CommandResult(False, f"You only have {my_tokens} tokens.")
                
            self.spend_tokens(agent_ref, amount)
            self.add_tokens(target.dbref, amount)
            
            return CommandResult(
                True, 
                f"You gave **{amount} tokens** to **{target.name}**.",
                message_3p=f"**{agent.name}** gives **{amount} tokens** to **{target.name}**.",
                context={'action': 'give'}
            )
        else:
            # ITEM LOGIC (Inventory Check)
            item = None
            for ref in agent.inventory:
                obj = self.db.get(ref)
                if obj and (obj.name.lower() == gift_str.lower() or obj.dbref == gift_str):
                    item = obj
                    break
            
            if not item:
                return CommandResult(False, f"You aren't carrying '{gift_str}'.")
                
            # Transfer
            agent.inventory.remove(item.dbref)
            target.inventory.append(item.dbref)
            
            # Update Location Index logic
            if item.location in self.db._location_index:
                if item.dbref in self.db._location_index[item.location]:
                    self.db._location_index[item.location].remove(item.dbref)
            
            item.location = target.dbref
            
            # Add to new location index
            if target.dbref not in self.db._location_index:
                self.db._location_index[target.dbref] = []
            self.db._location_index[target.dbref].append(item.dbref)
            
            return CommandResult(
                True,
                f"You gave **{item.name}** to **{target.name}**.",
                message_3p=f"**{agent.name}** gives **{item.name}** to **{target.name}**.",
                context={'action': 'give', 'item': item.to_dict(), 'to': target.to_dict()}
            )
    
    # ─────────────────────────────────────────────────────────────
    # Ownership & Lock Commands
    # ─────────────────────────────────────────────────────────────
    
    def _cmd_examine(self, agent_ref: str, args: str) -> CommandResult:
        """Examine an object in detail, showing ownership and lock."""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        if not args:
            return CommandResult(False, "Examine what?")
        
        target, err = self._resolve_sense_target(agent, args)
        if err:
            return CommandResult(False, err)
        
        lines = [f"**{target.name}** ({target.dbref})"]
        lines.append(f"Type: {target.type}")
        lines.append(f"Owner: {self.get_owner_name(target)} ({getattr(target, 'owner', 'none')})")
        
        lock = getattr(target, 'lock', '') or ''
        lines.append(f"Lock: {lock if lock else '(unlocked)'}")
        
        if getattr(target, 'enter_ok', False):
            lines.append("Enterable: YES")
        if getattr(target, 'ai_ok', False):
            lines.append("AI-Ok: YES")
        if getattr(target, 'search_ok', False):
            lines.append("Search-Ok: YES")
        if getattr(target, 'robot', False):
            lines.append("Robot: YES")
            
        v_type = getattr(target, 'vehicle_type', '')
        if v_type:
            lines.append(f"Vehicle Type: {v_type}")

        
        if getattr(target, 'memo', ''):
            lines.append(f"\n**Persistent Memo:**\n{target.memo}")
        
        if target.desc:
            lines.append(f"\n{target.desc}")
        
        # Show sensory attributes if set
        senses = []
        if getattr(target, 'olfactory', ''):
            senses.append(f"Smell: {target.olfactory}")
        if getattr(target, 'auditory', ''):
            senses.append(f"Sound: {target.auditory}")
        if getattr(target, 'tactile', ''):
            senses.append(f"Texture: {target.tactile}")
        if getattr(target, 'flavor', ''):
            senses.append(f"Taste: {target.flavor}")
        
        if senses:
            lines.append("\n**Senses:**")
            lines.extend([f"  {s}" for s in senses])
            
        # Show AI Reaction Triggers
        ai_triggers = []
        if getattr(target, 'adesc', ''): ai_triggers.append(f"adesc: {target.adesc}")
        if getattr(target, 'asmell', ''): ai_triggers.append(f"asmell: {target.asmell}")
        if getattr(target, 'ataste', ''): ai_triggers.append(f"ataste: {target.ataste}")
        if getattr(target, 'atouch', ''): ai_triggers.append(f"atouch: {target.atouch}")
        if getattr(target, 'alisten', ''): ai_triggers.append(f"alisten: {target.alisten}")
        
        if ai_triggers:
            lines.append("\n**AI Reactions:**")
            lines.extend([f"  {t}" for t in ai_triggers])
            
        # Show custom attributes
        if target.attrs:
            lines.append("\n**Attributes:**")
            for k, v in target.attrs.items():
                lines.append(f"  &{k.upper()} {target.dbref}=`{v}`")
        
        # Show Contents / Inventory
        contents = self.db.get_room_contents(target.dbref)
        if contents:
            if target.type == 'agent':
                lines.append("\n**Inventory:**")
            else:
                lines.append("\n**Contents:**")
            
            for item in contents:
                eval_name = self._evaluate_functions(agent_ref, item.name, item.dbref)
                lines.append(f"  {eval_name} ({item.dbref})")
        
        return CommandResult(True, "\n".join(lines))
    
    def _cmd_lock(self, agent_ref: str, args: str) -> CommandResult:
        """Lock an object. Syntax: @lock <target>=<lock expression>"""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        if '=' not in args:
            return CommandResult(False, "Usage: `@lock <target>=<lock>`\nLock types: `#dbref`, `wizard`, `!wizard`, `object:#dbref`")
        
        target_str, lock_expr = args.split('=', 1)
        target_str = target_str.strip()
        lock_expr = lock_expr.strip()
        
        target, err = self._resolve_sense_target(agent, target_str)
        if err:
            return CommandResult(False, err)
        
        # Check ownership
        if not self.can_modify(agent_ref, target):
            return CommandResult(False, f"You don't own **{target.name}**.")
        
        target.lock = lock_expr
        return CommandResult(
            True, 
            f"Locked **{target.name}** with: `{lock_expr}`",
            message_3p=f"**{agent.name}** locked **{target.name}**."
        )
    
    def _cmd_unlock(self, agent_ref: str, args: str) -> CommandResult:
        """Remove lock from an object. Syntax: @unlock <target>"""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        if not args:
            return CommandResult(False, "Unlock what?")
        
        target, err = self._resolve_sense_target(agent, args)
        if err:
            return CommandResult(False, err)
        
        # Check ownership
        if not self.can_modify(agent_ref, target):
            return CommandResult(False, f"You don't own **{target.name}**.")
        
        target.lock = ""
        return CommandResult(
            True, 
            f"Unlocked **{target.name}**.",
            message_3p=f"**{agent.name}** unlocked **{target.name}**."
        )
    
    def _cmd_chown(self, agent_ref: str, args: str) -> CommandResult:
        """Transfer ownership. Syntax: @chown <target>=<player>. Wizard only."""
        agent = self.db.get_agent(agent_ref)
        if not agent:
            return CommandResult(False, "You don't exist!")
        
        if not getattr(agent, 'wizard', False):
            return CommandResult(False, "Permission denied. Wizard powers required.")
        
        if '=' not in args:
            return CommandResult(False, "Usage: `@chown <target>=<player name or #dbref>`")
        
        target_str, new_owner_str = args.split('=', 1)
        target_str = target_str.strip()
        new_owner_str = new_owner_str.strip()
        
        target, err = self._resolve_sense_target(agent, target_str)
        if err:
            return CommandResult(False, err)
        
        # Find new owner
        new_owner = None
        if new_owner_str.startswith('#'):
            new_owner = self.db.get_agent(new_owner_str)
        else:
            for obj in self.db.objects.values():
                if obj.type == 'agent' and obj.name.lower() == new_owner_str.lower():
                    new_owner = obj
                    break
        
        if not new_owner:
            return CommandResult(False, f"Player '{new_owner_str}' not found.")
        
        old_owner = self.get_owner_name(target)
        target.owner = new_owner.dbref
        
        return CommandResult(True, f"Transferred **{target.name}** from {old_owner} to **{new_owner.name}**.")

    def _announce_arrival(self, agent_ref: str, room_ref: str, message: str):
        """Broadcast an arrival message to everyone in the destination room."""
        agent = self.db.get_agent(agent_ref)
        # Use getattr to safely check 'sneaky'
        if not agent or getattr(agent, 'sneaky', False):
            return
            
        arrival_text = f"✨ **{agent.name}** {message}"
        
        # Broadcast to room so others see it (exclude self to prevent echo)
        self.db.room_announce(room_ref, arrival_text, exclude=agent_ref)
        
        # Add to history so AI/UI can see it
        self._add_to_history(agent_ref, "PRESENCE_ARRIVAL", arrival_text)
        
    def _announce_departure(self, agent_ref: str, room_ref: str, message: str):
        """Broadcast a departure message to everyone in the source room."""
        agent = self.db.get_agent(agent_ref)
        # Use getattr to safely check 'sneaky'
        if not agent or getattr(agent, 'sneaky', False):
            return
            
        departure_text = f"✨ **{agent.name}** {message}"
        
        # Broadcast to room so others see it (exclude self to prevent echo)
        self.db.room_announce(room_ref, departure_text, exclude=agent_ref)
        
        # Add to history so AI/UI can see it
        self._add_to_history(agent_ref, "PRESENCE_DEPARTURE", departure_text)

    def _cmd_home(self, agent_ref: str, args: str) -> CommandResult:
        """Return to your home room."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        home_ref = agent.home
        if not home_ref:
            return CommandResult(False, "You have no home set. Use `@home me=<#dbref>`.")
            
        home_room = self.db.get_room(home_ref)
        if not home_room:
            return CommandResult(False, f"Your home ({home_ref}) no longer exists.")
            
        if agent.location == home_ref:
            return CommandResult(False, "You are already home.")
            
        # Departure
        self._announce_departure(agent_ref, agent.location, "has gone home.")
        
        # Move
        self.db.move_agent(agent_ref, home_ref)
        
        # Arrival
        self._announce_arrival(agent_ref, home_ref, "has arrived home.")
        
        look_result = self._cmd_look(agent_ref, "")
        return CommandResult(True, f"There's no place like home.\n\n{look_result.message}")

    def _cmd_teleport(self, agent_ref: str, args: str) -> CommandResult:
        """Teleport an object or self to a destination. Syntax: @tel <target>=<#dest> or @tel <#dest>"""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        # Wizard-only
        if not getattr(agent, 'wizard', False):
            return CommandResult(False, "Only wizards can teleport.")
        
        # Parse: @tel me=#5 OR @tel #5 (teleport self)
        if '=' in args:
            target_str, dest_str = args.split('=', 1)
        else:
            target_str = 'me'
            dest_str = args
        
        target = self.match_object(agent_ref, target_str.strip())
        if not target:
            return CommandResult(False, f"I don't see '{target_str.strip()}' here.")
        
        dest = self.db.get(dest_str.strip())
        if not dest:
            return CommandResult(False, f"Destination '{dest_str.strip()}' not found.")
        
        old_loc = target.location
        
        # For agents, use move_agent; for objects, set location directly
        if target.type == 'agent':
            self._announce_departure(target.dbref, old_loc, "vanishes in a flash of light.")
            self.db.move_agent(target.dbref, dest.dbref)
            self._announce_arrival(target.dbref, dest.dbref, "appears in a flash of light.")
        else:
            target.location = dest.dbref
        
        # If we teleported ourselves, show the new location
        if target.dbref == agent_ref:
            look_result = self._cmd_look(agent_ref, "")
            return CommandResult(True, f"*WHOOSH* You teleport to **{dest.name}**.\n\n{look_result.message}")
        else:
            return CommandResult(True, f"*WHOOSH* **{target.name}** teleported to **{dest.name}** ({dest.dbref}).")

    def _cmd_at_home(self, agent_ref: str, args: str) -> CommandResult:
        """Set an object's home room. Syntax: @home <target>=<#dbref>"""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        if '=' not in args:
            return CommandResult(False, "Usage: `@home <target>=<#dbref>`")
            
        target_str, home_ref = args.split('=', 1)
        target = self.match_object(agent_ref, target_str.strip())
        if not target:
            return CommandResult(False, f"I don't see '{target_str.strip()}' here.")
            
        if not self.can_modify(agent_ref, target):
            return CommandResult(False, f"You don't own **{target.name}**.")
            
        home_room = self.db.get_room(home_ref.strip())
        if not home_room:
            return CommandResult(False, f"'{home_ref.strip()}' is not a valid room.")
            
        target.home = home_room.dbref
        return CommandResult(True, f"The home of **{target.name}** is now **{home_room.name}** ({home_room.dbref}).")

    # ─────────────────────────────────────────────────────────────
    # NPC Automation Hook
    # ─────────────────────────────────────────────────────────────
    
    def get_autonomous_npcs_in_room(self, room_ref: str) -> List[GameObject]:
        """Get all autonomous NPCs in a room."""
        return self.db.get_autonomous_agents(room_ref)
    
    def process_npc_turn(self, npc_ref: str, action: str) -> CommandResult:
        """
        Process an NPC's action as if they typed it.
        
        This is called after the AI layer decides what the NPC should do.
        The action is a string like "say Welcome, traveler!" or "pose waves."
        """
        return self.process_command(npc_ref, action)

    def _cmd_who(self, agent_ref: str, args: str) -> CommandResult:
        """List connected agents."""
        import time
        now = time.time()
        
        # For our singleton model, we list human agents active in the last 5 minutes
        agents = []
        for obj in self.db.objects.values():
            if obj.type == 'agent' and not obj.autonomous:
                last = getattr(obj, 'last_interaction', 0)
                if (now - last) <= 300: # 5 minutes
                    agents.append(obj)
        
        if not agents:
            return CommandResult(True, "### 👥 Online Users\nNo active users.")

        # Sort by name
        agents.sort(key=lambda a: a.name.lower())

        msg = f"### 👥 Online Users ({len(agents)})\n"
        for a in agents:
            loc = self.db.get(a.location)
            loc_name = loc.name if loc else "Somewhere"
            msg += f"- **{a.name}** ({a.dbref}) - *In {loc_name}*\n"
            
        return CommandResult(True, msg)


    # ─────────────────────────────────────────────────────────────
    # Math & Date/Time Commands
    # ─────────────────────────────────────────────────────────────
    
    def _cmd_math_add(self, agent_ref: str, args: str) -> CommandResult:
        """Add two numbers."""
        try:
            parts = args.split()
            if len(parts) < 2:
                return CommandResult(False, "Usage: @add <a> <b>")
            result = float(parts[0]) + float(parts[1])
            return CommandResult(True, f"**Result:** {result}")
        except ValueError:
            return CommandResult(False, "Invalid numbers. Usage: @add <a> <b>")
    
    def _cmd_math_subtract(self, agent_ref: str, args: str) -> CommandResult:
        """Subtract b from a."""
        try:
            parts = args.split()
            if len(parts) < 2:
                return CommandResult(False, "Usage: @subtract <a> <b>")
            result = float(parts[0]) - float(parts[1])
            return CommandResult(True, f"**Result:** {result}")
        except ValueError:
            return CommandResult(False, "Invalid numbers. Usage: @subtract <a> <b>")
    
    def _cmd_math_multiply(self, agent_ref: str, args: str) -> CommandResult:
        """Multiply two numbers."""
        try:
            parts = args.split()
            if len(parts) < 2:
                return CommandResult(False, "Usage: @multiply <a> <b>")
            result = float(parts[0]) * float(parts[1])
            return CommandResult(True, f"**Result:** {result}")
        except ValueError:
            return CommandResult(False, "Invalid numbers. Usage: @multiply <a> <b>")
    
    def _cmd_math_divide(self, agent_ref: str, args: str) -> CommandResult:
        """Divide a by b."""
        try:
            parts = args.split()
            if len(parts) < 2:
                return CommandResult(False, "Usage: @divide <a> <b>")
            b = float(parts[1])
            if b == 0:
                return CommandResult(False, "#DIV/0! Division by zero.")
            result = float(parts[0]) / b
            return CommandResult(True, f"**Result:** {result}")
        except ValueError:
            return CommandResult(False, "Invalid numbers. Usage: @divide <a> <b>")
    
    def _cmd_date(self, agent_ref: str, args: str) -> CommandResult:
        """Show current date."""
        date_str = datetime.now().strftime('%A, %B %d, %Y')
        return CommandResult(True, f"📅 **Date:** {date_str}")
    
    def _cmd_time(self, agent_ref: str, args: str) -> CommandResult:
        """Show current time."""
        time_str = datetime.now().strftime('%I:%M %p').lstrip('0')
        return CommandResult(True, f"🕐 **Time:** {time_str}")

    def get_scene_context(self, agent_ref: str) -> Dict[str, Any]:
        """Collect context for scene visualization."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return {}
        
        room_ref = agent.location
        room = self.db.get(room_ref)
        if not room: return {}
        
        contents = self.db.get_room_contents(room_ref)
        room_data = room.to_dict()
        
        # VR OVERRIDE: If in VR room, use player's subjective description
        if getattr(room, 'vr_ok', False):
            vr_desc_key = f"_vr_desc_{agent_ref}"
            vr_title_key = f"_vr_title_{agent_ref}"
            
            # Use "VR Simulation" as a generic fallback for the name
            room_data['name'] = "VR Simulation"
            room_data['_vr_active'] = True
            
            if vr_desc_key in room.attrs:
                room_data['desc'] = room.attrs[vr_desc_key]
            if vr_title_key in room.attrs:
                room_data['name'] = room.attrs[vr_title_key]
        
        # Get last player action from history
        last_action = ""
        for h in reversed(self.history):
            if h.get('agent') == agent_ref and h.get('command') != 'AI_REACTION':
                last_action = h.get('command')
                break
                
        # Get most recent AI reactions
        recent_ai = []
        for h in reversed(self.history[-10:]):
            if h.get('command') == 'AI_REACTION':
                recent_ai.append(h.get('response'))
        
        return {
            'room': room_data,
            'contents': [c.to_dict() for c in contents],
            'last_action': last_action,
            'recent_ai': " ".join(recent_ai)
        }

    def _cmd_set_summon_ok(self, agent_ref: str, args: str) -> CommandResult:
        """Toggle summon_ok flag. Syntax: @summon_ok <target>=<yes|no>"""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        if '=' not in args:
            return CommandResult(False, "Usage: `@summon_ok <target>=<yes|no>`")
            
        target_str, val = args.split('=', 1)
        target = self.match_object(agent_ref, target_str.strip())
        if not target:
            return CommandResult(False, f"I don't see '{target_str.strip()}' here.")
            
        if not self.can_modify(agent_ref, target):
            return CommandResult(False, f"You don't own **{target.name}**.")
            
        is_enabled = val.strip().lower() in ['yes', 'true', '1', 'on']
        target.summon_ok = is_enabled
        status = "ENABLED" if is_enabled else "DISABLED"
        return CommandResult(True, f"Summoning for **{target.name}** is now {status}.")

    def _cmd_summon(self, agent_ref: str, args: str) -> CommandResult:
        """Summon an agent. Syntax: @summon <agent>"""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        if not args: return CommandResult(False, "Summon whom?")
        
        db = self.db
        
        # We need to find the agent globally, not just nearby
        target_ref = db._name_index.get(args.lower().strip())
        if not target_ref:
            # Try DBRef lookup
            if args.startswith('#') and args in db.objects:
                target_ref = args
            else:
                return CommandResult(False, f"Agent '{args}' not found.")
                
        target = db.get_agent(target_ref)
        if not target:
            return CommandResult(False, f"Target is not an agent.")
            
        if target.dbref == agent_ref:
            return CommandResult(False, "You cannot summon yourself.")

        # Check permission (Wizard overrides)
        is_wizard = getattr(agent, 'wizard', False)
        if not is_wizard and not getattr(target, 'summon_ok', False):
            # Also allow if owner summons
            if getattr(target, 'owner', '') != agent_ref:
                return CommandResult(False, f"**{target.name}** does not wish to be summoned.")
        
        # Same room check
        if target.location == agent.location:
             return CommandResult(False, f"**{target.name}** is already here.")

        # Move Logic
        old_loc = target.location
        old_room = db.get(old_loc)
        new_loc = agent.location
        new_room = db.get(new_loc)
        
        # Departure
        self._announce_departure(target.dbref, old_loc, f"has vanished in a swirl of light!")
        
        db.move_agent(target.dbref, new_loc)
        
        # Arrival
        self._announce_arrival(target.dbref, new_loc, f"appears in a swirl of light, summoned by **{agent.name}**!")
        
        # Return success to summoner
        return CommandResult(
            True, 
            f"You summoned **{target.name}**.",
            context={
                'action': 'summon',
                'target': target.to_dict()
            }
        )

    def check_idle_agents(self, timeout_seconds: int = 300) -> List[str]:
        """
        Check for idle autonomous agents and send them home.
        Returns a list of messages describing what happened.
        """
        import time
        now = time.time()
        db = self.db
        messages = []
        
        # Scan all autonomous agents
        for obj in db.objects.values():
            if obj.type == 'agent' and obj.autonomous:
                # Must have a home
                if not obj.home: continue
                
                # Must not be already at home
                if obj.location == obj.home: continue
                
                # Check interaction time
                last = getattr(obj, 'last_interaction', 0.0)
                if (now - last) > timeout_seconds:
                    
                    # Logic: Only go home if ALONE in the room (ignoring other bots?)
                    # Or maybe just if no PLAYERS are there?
                    # Let's say: If no players are present.
                    contents = db.get_room_contents(obj.location)
                    has_player = any(o.type == 'agent' and not o.autonomous for o in contents)
                    
                    if not has_player:
                        # Go home!
                        old_loc = obj.location
                        old_room = db.get(old_loc)
                        home_room = db.get(obj.home)
                        home_name = home_room.name if home_room else "Home"
                        
                        self._announce_departure(obj.dbref, old_loc, f"wanders off towards home.")
                        db.move_agent(obj.dbref, obj.home)
                        self._announce_arrival(obj.dbref, obj.home, f"arrives from **{old_room.name if old_room else 'somewhere'}**.")
                        
                        # Reset interaction to avoid immediate re-check loop (though location change helps)
                        obj.last_interaction = now
                        
                        msg = f"[Idle] Sent **{obj.name}** from {old_room.name if old_room else '???'} to {home_name}."
                        messages.append(msg)
                        print(msg)
                        
        return messages

    def _cmd_outfit(self, agent_ref: str, args: str) -> CommandResult:
        """
        Manage outfits. 
        Syntax: 
          @outfit list [target]
          @outfit define [target] <1-10>=<desc>
        """
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        args = args.strip()
        if not args:
            return CommandResult(False, "Usage: `@outfit define [target] <1-10>=<desc>` or `@outfit list [target]`")
            
        parts = args.split(None, 1)
        subcmd = parts[0].lower()
        subargs = parts[1] if len(parts) > 1 else ""
        
        # Helper to resolve target
        target = agent
        cmd_args = subargs
        
        if subargs:
            # Check if first word is a target
            # Heuristic: If it looks like "1=..." or just "1", it's a slot, so target is ME.
            # Otherwise, it's likely a target name.
            first_word = subargs.split()[0]
            is_slot = False
            # Check for "N" or "N=..." where N is digit
            if first_word.isdigit():
                 is_slot = True
            elif '=' in first_word and first_word.split('=')[0].isdigit():
                 is_slot = True
            
            if not is_slot and subcmd == 'define':
                # Try to match target
                # We need to split subargs: "Target Name 1=Desc" -> "Target Name", "1=Desc"
                # This is tricky because names can have spaces.
                # However, the slot part "1=..." is distinct.
                # We can rsplit by space? No, "1=..." might contain spaces in desc.
                # But the SLOT definition "1=" is tight.
                import re
                match = re.search(r'\s(\d+)=', subargs)
                if match:
                    split_idx = match.start()
                    target_name = subargs[:split_idx].strip()
                    cmd_args = subargs[split_idx+1:].strip()
                    
                    found = self.match_object(agent_ref, target_name)
                    if found:
                        target = found
            
            elif not is_slot and subcmd == 'list':
                # For list, it's just the name
                found = self.match_object(agent_ref, subargs)
                if found:
                    target = found
                    cmd_args = "" # consumed
        
        # Permission Check
        if target != agent and not self.can_modify(agent_ref, target):
             return CommandResult(False, f"You don't own **{target.name}**.")

        # Logic
        if subcmd == 'list':
            lines = [f"**Wardrobe for {target.name}:**"]
            found_outfit = False
            for i in range(1, 11):
                attr_name = f"outfit_{i}"
                if attr_name in target.attrs:
                    desc = target.attrs[attr_name]
                    preview = (desc[:50] + '...') if len(desc) > 50 else desc
                    lines.append(f"  **{i}:** {preview}")
                    found_outfit = True
            if not found_outfit:
                 lines.append("  (Empty)")
            
            return CommandResult(True, "\n".join(lines))
            
        elif subcmd == 'define':
             if '=' not in cmd_args:
                 return CommandResult(False, "Usage: `@outfit define [target] <n>=<description>`")
             
             slot_str, desc = cmd_args.split('=', 1)
             try:
                 slot = int(slot_str.strip())
                 if not (1 <= slot <= 10):
                     raise ValueError
             except ValueError:
                 return CommandResult(False, "Slot must be a number between 1 and 10.")
                 
             target.attrs[f"outfit_{slot}"] = desc.strip()
             return CommandResult(True, f"Outfit {slot} defined for **{target.name}**.")
             
        return CommandResult(False, "Unknown subcommand.")

    def _cmd_wear(self, agent_ref: str, args: str) -> CommandResult:
        """
        Wear an outfit. 
        Syntax: 
          @wear [target] <1-10>
          @wear [target] <description string> (Magic Wear)
        """
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist!")
        
        if not args:
             return CommandResult(False, "Usage: `@wear [target] <1-10|description>`")
             
        args = args.strip()
        
        # 1. Determine Target and Argument
        # Logic: Check if args starts with the name of an agent I own (or me)
        # Sort owned agents by name length desc to match longest first
        owned_agents = [agent]
        for obj in self.db.objects.values():
            if obj.type == 'agent' and getattr(obj, 'owner', '') == agent_ref and obj.dbref != agent_ref:
                owned_agents.append(obj)
        
        # Sort by name length desc to match "Lexi Bot" before "Lexi"
        owned_agents.sort(key=lambda x: len(x.name), reverse=True)
        
        target = agent
        wear_arg = args
        
        for pot_target in owned_agents:
            # Case insensitive match for command ergonomics
            # Check if args starts with "Name " (space is important unless exact match)
            pt_name = pot_target.name.lower()
            args_lower = args.lower()
            
            if args_lower == pt_name:
                # "@wear Lexi" -> Missing arg? Or maybe implied?
                # Usually we expect an arg. treating as error for now.
                pass 
            elif args_lower.startswith(pt_name + " "):
                # Match! context is the rest
                target = pot_target
                wear_arg = args[len(pt_name)+1:].strip() # Slice original string to preserve case
                break
                
        # Permission global check (redundant if we only selected from owned, but good for safety if we change logic)
        if target != agent and not self.can_modify(agent_ref, target):
             return CommandResult(False, f"You don't own **{target.name}**.")

        # 2. Determine Mode: Slot vs Magic String
        slot = None
        description = None
        
        # Is it a number 1-10?
        try:
            val = int(wear_arg)
            if 1 <= val <= 10:
                slot = val
        except ValueError:
            pass
            
        if slot:
            # SLOT MODE
            attr_name = f"outfit_{slot}"
            if attr_name not in target.attrs:
                return CommandResult(False, f"Outfit {slot} is not defined for **{target.name}**.")
            description = target.attrs[attr_name]
            msg = f"**{target.name}** is now wearing Outfit {slot}."
        else:
            # MAGIC WEAR MODE (String)
            # Slot 11 is the "Magic Slot" (Infinite Closet)
            slot = 11
            description = wear_arg
            
            # Save it to outfit_11
            target.attrs["outfit_11"] = description
            msg = f"**{target.name}** manifests a new outfit."

        # 3. Apply
        if not description:
             return CommandResult(False, "Outfit description is empty.")
             
        target.desc = description
        
        self.db.room_announce(target.location, f"🌐\n**{target.name}** changes their outfit.")
        
        return CommandResult(
            True, 
            msg,
            context={'action': 'wear', 'desc': description}
        )

    def _cmd_reset_vr(self, agent_ref: str, args: str) -> CommandResult:
        """Manually reset the VR state for the current room."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist.")
        
        room = self.db.get_room(agent.location)
        if not room: return CommandResult(False, "You are nowhere.")
        
        if not getattr(room, 'vr_ok', False):
            return CommandResult(False, "This room is not VR-enabled.")
        
        # Clear THIS player's VR description (their subjective experience)
        vr_desc_key = f"_vr_desc_{agent.dbref}"
        if vr_desc_key in room.attrs:
            del room.attrs[vr_desc_key]
            return CommandResult(True, "VR simulation reset. Reality stabilizes.")
        else:
            return CommandResult(False, "You don't have an active VR overlay.")
    
    def _cmd_vr_memo(self, agent_ref: str, args: str) -> CommandResult:
        """Set VR room's persistent context. Owner can target remotely."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist.")
        
        # Parse room=text syntax
        room = None
        text = args
        
        if '=' in args:
            parts = args.split('=', 1)
            room_name = parts[0].strip()
            text = parts[1].strip()
            
            # Find room by dbref or name
            room = self.match_object(agent_ref, room_name)
            if not room:
                return CommandResult(False, f"Room not found: '{room_name}'")
            if room.type != 'room':
                return CommandResult(False, f"**{room.name}** is not a room.")
        else:
            # Default to current room
            room = self.db.get_room(agent.location)
            if not room: return CommandResult(False, "You are nowhere.")
        
        # Permission check (owner or wizard)
        if not self.can_modify(agent_ref, room):
            return CommandResult(False, f"You don't own **{room.name}**.")
        
        if not getattr(room, 'vr_ok', False):
            return CommandResult(False, f"**{room.name}** is not VR-enabled. Use `@vr_ok {room.dbref}=yes` first.")
        
        vr_memo_key = "_vr_memo"  # Room-level, not per-player
        
        if not text:
            current = room.attrs.get(vr_memo_key, "(empty)")
            return CommandResult(True, f"**VR Memory for {room.name}:** {current}")
        
        room.attrs[vr_memo_key] = text
        return CommandResult(True, f"VR Memory set for **{room.name}**: {text}")
    
    def _cmd_vr_intent(self, agent_ref: str, args: str) -> CommandResult:
        """Set VR room's narrative goal. Owner can target remotely."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist.")
        
        # Parse room=text syntax
        room = None
        text = args
        
        if '=' in args:
            parts = args.split('=', 1)
            room_name = parts[0].strip()
            text = parts[1].strip()
            
            room = self.match_object(agent_ref, room_name)
            if not room:
                return CommandResult(False, f"Room not found: '{room_name}'")
            if room.type != 'room':
                return CommandResult(False, f"**{room.name}** is not a room.")
        else:
            room = self.db.get_room(agent.location)
            if not room: return CommandResult(False, "You are nowhere.")
        
        if not self.can_modify(agent_ref, room):
            return CommandResult(False, f"You don't own **{room.name}**.")
        
        if not getattr(room, 'vr_ok', False):
            return CommandResult(False, f"**{room.name}** is not VR-enabled.")
        
        vr_intent_key = "_vr_intent"  # Room-level
        
        if not text:
            current = room.attrs.get(vr_intent_key, "(none)")
            return CommandResult(True, f"**VR Intent for {room.name}:** {current}")
        
        room.attrs[vr_intent_key] = text
        return CommandResult(True, f"VR Intent set for **{room.name}**: {text}")
    
    def _cmd_vr_clear(self, agent_ref: str, args: str) -> CommandResult:
        """Wipe all VR state from a room. Owner can target remotely."""
        agent = self.db.get_agent(agent_ref)
        if not agent: return CommandResult(False, "You don't exist.")
        
        # Parse optional room target
        room = None
        if args:
            room = self.match_object(agent_ref, args.strip())
            if not room:
                return CommandResult(False, f"Room not found: '{args}'")
            if room.type != 'room':
                return CommandResult(False, f"**{room.name}** is not a room.")
        else:
            room = self.db.get_room(agent.location)
            if not room: return CommandResult(False, "You are nowhere.")
        
        if not self.can_modify(agent_ref, room):
            return CommandResult(False, f"You don't own **{room.name}**.")
        
        # Clear room-level VR keys
        keys_to_clear = ["_vr_memo", "_vr_intent", "_vr_desc"]
        
        # Also clear any per-player legacy keys
        legacy_prefixes = ["_vr_desc_", "_vr_memo_", "_vr_intent_", "_vr_"]
        for key in list(room.attrs.keys()):
            for prefix in legacy_prefixes:
                if key.startswith(prefix):
                    keys_to_clear.append(key)
        
        cleared_count = 0
        for key in keys_to_clear:
            if key in room.attrs:
                del room.attrs[key]
                cleared_count += 1
        
        if cleared_count > 0:
            return CommandResult(True, f"VR state cleared for **{room.name}** ({cleared_count} entries). Baseline reality restored.")
        else:
            return CommandResult(False, f"No VR state to clear in **{room.name}**.")


    # Update interaction timestamp on command processing
    def update_interaction(self, agent_ref: str):
        """Update last_interaction timestamp for agent."""
        import time
        now = time.time()
        agent = self.db.get_agent(agent_ref)
        if agent:
             agent.last_interaction = now
             # Removed bystander updates to allow idle detection to work correctly for 'offline' players


# ─────────────────────────────────────────────────────────────────
# Quick test when run directly
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from pathlib import Path
    
    # Load test world
    db = WorldDatabase()
    world_path = Path(__file__).parent / "world.json"
    
    if world_path.exists():
        db.load(world_path)
        print(f"Loaded world: {db.meta.get('name')}")
    else:
        print("No world.json found, creating empty world")
    
    engine = MashEngine(db)
    player_ref = "#1" # Mogura exists
    
    # Test commands
    print("\n--- Testing look ---")
    result = engine.process_command(player_ref, "look")
    print(result.message)
    
    print("\n--- Testing home ---")
    result = engine.process_command(player_ref, "home")
    print(result.message)
    
    print("\n--- Testing go elevator ---")
    result = engine.process_command(player_ref, "go elevator")
    print(result.message)

