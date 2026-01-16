"""
MASH Database Module
====================
Manages the world state: rooms, exits, objects, and agents.
All data is stored in a single JSON file.

Credit: Inspired by TinyMUSH flat-file database architecture.
"""

import json
import os
import uuid
import threading
import tempfile
from pathlib import Path
from typing import Optional, Dict, List, Any, Callable
from dataclasses import dataclass, field, asdict


@dataclass
class GameObject:
    """Base class for all game objects."""
    dbref: str
    type: str
    name: str
    desc: str = ""
    location: str = "" # Added base location field for consistency
    
    # Room-specific
    exits: List[str] = field(default_factory=list)
    contents: List[str] = field(default_factory=list)
    
    # Exit-specific
    aliases: List[str] = field(default_factory=list)
    source: str = ""
    destination: str = ""
    
    # Agent-specific
    autonomous: bool = False # Restored for consistency
    robot: bool = False
    search_ok: bool = False # AI grounding via Google Search
    summon_ok: bool = False # Allow being summoned
    home: str = "" # Re-standardized as str
    last_interaction: float = 0.0 # Interaction tracking for auto-home
    inventory: List[str] = field(default_factory=list)
    password_hash: str = ""
    wizard: bool = False
    tokens: int = 0
    message_buffer: List[str] = field(default_factory=list) # Async message queue
    
    # Object ownership and permissions
    owner: str = ""
    lock: str = ""
    
    # Sensory
    olfactory: str = ""
    flavor: str = ""
    tactile: str = ""
    auditory: str = ""
    
    # AI Reactions
    adesc: str = ""
    asmell: str = ""
    ataste: str = ""
    atouch: str = ""
    alisten: str = ""
    memo: str = ""
    status: str = "" # UPSUM / Narrative Goals
    listening: bool = False
    
    # Custom
    attrs: Dict[str, str] = field(default_factory=dict)
    
    # Flags
    enter_ok: bool = False
    ai_ok: bool = False
    sneaky: bool = False
    vehicle_type: str = ""  # e.g., "boat", "aircraft", "mech"
    vr_ok: bool = False  # Enable Subjective VR Reality for rooms

    
    def inventory_objects(self, db: 'WorldDatabase') -> List['GameObject']:
        """Helper to get actual objects from inventory dbrefs."""
        items = []
        for ref in self.inventory:
            obj = db.get(ref)
            if obj:
                items.append(obj)
        return items

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, excluding empty fields."""
        data = asdict(self)
        # Remove dbref from dict (it's the key, not a value)
        del data['dbref']
        # Remove empty fields for cleaner JSON
        return {k: v for k, v in data.items() if v or k == 'type'}


class WorldDatabase:
    """
    Manages the game world state.
    
    Objects are stored with string dbrefs like "#1", "#2", etc.
    Now includes indices for scalable lookups.
    """
    
    def __init__(self):
        self._lock = threading.RLock() # Thread-safety for multi-player/concurrent access
        self.instance_id = str(uuid.uuid4())
        self.objects: Dict[str, GameObject] = {}
        self.meta: Dict[str, Any] = {"version": "1.0", "name": "Unnamed World"}
        self.next_dbref: int = 0
        self.on_announce: Optional[Callable[[str, str], None]] = None  # Callback for sync hooks
        
        # Indices for O(1) performance
        self._name_index: Dict[str, str] = {}  # name.lower() -> dbref
        self._type_index: Dict[str, List[str]] = {}  # type -> [dbrefs]
        self._location_index: Dict[str, List[str]] = {}  # location_dbref -> [content_dbrefs]
        self._room_announcements: Dict[str, List[str]] = {} # room -> [messages]
        self.free_dbrefs: List[int] = [] # Pool of recycled DBRef IDs
        with self._lock:
            self.rebuild_indices()
    
    def rebuild_indices(self) -> None:
        """Clear and rebuild all indices from the current objects dictionary."""
        self._name_index.clear()
        self._type_index.clear()
        self._location_index.clear()
        
        for dbref, obj in self.objects.items():
            # Name index (note: non-unique names will overwrite, that's okay for lookup priority)
            self._name_index[obj.name.lower()] = dbref
            
            # Type index
            if obj.type not in self._type_index:
                self._type_index[obj.type] = []
            self._type_index[obj.type].append(dbref)
            
            # Location index (for room contents)
            loc = getattr(obj, 'location', None)
            if loc:
                if loc not in self._location_index:
                    self._location_index[loc] = []
                self._location_index[loc].append(dbref)
    
    # ─────────────────────────────────────────────────────────────
    # File I/O
    # ─────────────────────────────────────────────────────────────
        
    def load(self, path: str):
        """Load the world from a JSON file."""
        if not os.path.exists(path):
            return
            
        with self._lock:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.meta = data.get("meta", self.meta)
                self.next_dbref = data.get("next_dbref", 1)
                self.free_dbrefs = data.get('free_dbrefs', []) # Retained from original
                
                # Get valid fields for GameObject
                valid_fields = set(GameObject.__dataclass_fields__.keys())
                
                self.objects = {}
                for dbref, obj_data in data["objects"].items():
                    # Filter out stale fields (like removed 'research_ok')
                    filtered_data = {k: v for k, v in obj_data.items() if k in valid_fields}
                    self.objects[dbref] = GameObject(dbref=dbref, **filtered_data)
                    
            self.rebuild_indices()
    
    def save(self, path: Path) -> None:
        """Save world to JSON file atomically."""
        with self._lock:
            data = {
                'meta': self.meta,
                'next_dbref': self.next_dbref,
                'free_dbrefs': self.free_dbrefs,
                'objects': {dbref: obj.to_dict() for dbref, obj in self.objects.items()}
            }
            
            # Atomic Write Strategy (Temp file + Rename)
            # This prevents corruption if the process crashes mid-save.
            temp_path = str(path) + ".tmp"
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
                # Atomic swap!
                os.replace(temp_path, path)
            except Exception as e:
                if os.path.exists(temp_path):
                    try: os.remove(temp_path)
                    except: pass
                raise e
    
    # ─────────────────────────────────────────────────────────────
    # Object Retrieval
    # ─────────────────────────────────────────────────────────────
    
    def get(self, dbref: str) -> Optional[GameObject]:
        """Get any object by dbref."""
        with self._lock:
            return self.objects.get(dbref)
    
    def get_room(self, dbref: str) -> Optional[GameObject]:
        """Get a room by dbref."""
        with self._lock:
            obj = self.get(dbref)
            return obj if obj and obj.type == 'room' else None
    
    def get_exit(self, dbref: str) -> Optional[GameObject]:
        """Get an exit by dbref."""
        with self._lock:
            obj = self.get(dbref)
            return obj if obj and obj.type == 'exit' else None
    
    def get_agent(self, dbref: str) -> Optional[GameObject]:
        """Get an agent by dbref."""
        with self._lock:
            obj = self.get(dbref)
            return obj if obj and obj.type == 'agent' else None
    
    # ─────────────────────────────────────────────────────────────
    # Room Queries
    # ─────────────────────────────────────────────────────────────
    
    def get_room_exits(self, room_ref: str) -> List[GameObject]:
        """Get all exit objects for a room."""
        room = self.get_room(room_ref)
        if not room:
            return []
        return [self.get_exit(ref) for ref in room.exits if self.get_exit(ref)]
    
    def get_room_contents(self, room_ref: str) -> List[GameObject]:
        """Get all agents/objects in a room using the location index."""
        with self._lock:
            dbrefs = self._location_index.get(room_ref, [])
            return [self.objects[ref] for ref in dbrefs if ref in self.objects]
    
    def get_autonomous_agents(self, room_ref: str) -> List[GameObject]:
        """Get all autonomous agents in a room."""
        with self._lock:
            return [a for a in self.get_room_contents(room_ref) if a.autonomous]
    
    # ─────────────────────────────────────────────────────────────
    # State Mutations
    # ─────────────────────────────────────────────────────────────
    
    def move_agent(self, agent_ref: str, destination_ref: str) -> bool:
        """Move an object/agent to a new location. Maintains location index."""
        with self._lock:
            obj = self.get(agent_ref)
            dest = self.get(destination_ref)
            if not obj or not dest:
                return False
                
            old_loc = obj.location
            obj.location = destination_ref
            
            # Update location index
            if old_loc in self._location_index:
                if agent_ref in self._location_index[old_loc]:
                    self._location_index[old_loc].remove(agent_ref)
                    
            if destination_ref not in self._location_index:
                self._location_index[destination_ref] = []
            self._location_index[destination_ref].append(agent_ref)
            
            return True
    
    def create_object(self, obj_type: str, name: str, **kwargs) -> GameObject:
        """Create a new object and return it. Maintains indices and recycles IDs."""
        with self._lock:
            # 1. Try recycling first, but verify the ID is actually free
            while self.free_dbrefs:
                self.free_dbrefs.sort()
                candidate_id = self.free_dbrefs.pop(0)
                candidate_ref = f"#{candidate_id}"
                
                # CRITICAL SAFETY CHECK: Zombie Prevention
                if candidate_ref not in self.objects:
                    dbid = candidate_id
                    break
                else:
                    # If it WAS in objects, it wasn't actually free. Skip it.
                    pass
            else:
                # 2. No free IDs (or all were zombies), use next_dbref
                # CRITICAL SAFETY CHECK: Time Travel Prevention
                # Loop until we find a dbref that does NOT exist
                while f"#{self.next_dbref}" in self.objects:
                    self.next_dbref += 1
                
                dbid = self.next_dbref
                self.next_dbref += 1
                
            dbref = f"#{dbid}"
            obj = GameObject(dbref=dbref, type=obj_type, name=name, **kwargs)
            self.objects[dbref] = obj
            
            # Update indices
            self._name_index[name.lower()] = dbref
            if obj_type not in self._type_index:
                self._type_index[obj_type] = []
            self._type_index[obj_type].append(dbref)
            
            loc = kwargs.get('location')
            if loc:
                if loc not in self._location_index:
                    self._location_index[loc] = []
                self._location_index[loc].append(dbref)
                
            return obj

    def destroy_object(self, dbref: str) -> bool:
        """
        Permanently delete an object from the world.
        Returns True if successful.
        """
        if dbref not in self.objects:
            return False
            
        obj = self.objects[dbref]
        
        # 1. Clean up from indices
        name_lower = obj.name.lower()
        if self._name_index.get(name_lower) == dbref:
            del self._name_index[name_lower]
            
        if obj.type in self._type_index and dbref in self._type_index[obj.type]:
            self._type_index[obj.type].remove(dbref)
            
        loc = getattr(obj, 'location', None)
        if loc in self._location_index and dbref in self._location_index[loc]:
            self._location_index[loc].remove(dbref)
            
        # 2. Recycle the ID
        try:
            dbid = int(dbref[1:])
            if dbid not in self.free_dbrefs:
                self.free_dbrefs.append(dbid)
        except ValueError:
            pass # Non-standard dbref, skip recycling
            
        # 3. Finally remove from main store
        del self.objects[dbref]
        return True
    
    # ─────────────────────────────────────────────────────────────
    # Utility
    # ─────────────────────────────────────────────────────────────
    
    def find_exit_by_name(self, room_ref: str, exit_name: str) -> Optional[GameObject]:
        """Find an exit in a room by name, alias, or unique prefix."""
        if not room_ref or not exit_name: return None
        
        # Robust normalization
        if not str(room_ref).startswith('#'): room_ref = f"#{room_ref}"
        
        room = self.get(room_ref)
        if not room: return None
        
        target = exit_name.lower().strip()
        
        # Iterate over exits directly to be safe
        for ref in room.exits:
            if not ref.startswith('#'): ref = f"#{ref}"
            e = self.get(ref)
            if not e or e.type != 'exit': continue
            
            # 1. Exact Name
            if e.name.lower() == target:
                return e
                
            # 2. Aliases (List)
            if hasattr(e, 'aliases') and e.aliases:
                if any(str(alias).lower() == target for alias in e.aliases):
                    return e
                    
            # 3. Prefix
            if e.name.lower().startswith(target):
                return e

            # 4. Substring (Relaxed)
            if target in e.name.lower():
                return e
        
        # --- GLOBAL FAIL-SAFE ---
        # If the local list missed it, hunt for any exit that claims this room as its source.
        for obj in self.objects.values():
            if obj.type == 'exit' and obj.source == room_ref:
                if obj.name.lower() == target: return obj
                if hasattr(obj, 'aliases') and obj.aliases:
                    if any(str(a).lower() == target for a in obj.aliases): return obj
                if obj.name.lower().startswith(target): return obj
                if target in obj.name.lower(): return obj
                
        return None

    # ─────────────────────────────────────────────────────────────
    # Announcement Logic
    # ─────────────────────────────────────────────────────────────

    def room_announce(self, room_ref: str, message: str, exclude: Optional[str] = None) -> None:
        """Log an announcement to a room and broadcast to occupants."""
        with self._lock:
            # 1. Log for triggers (Legacy/AI)
            if room_ref not in self._room_announcements:
                self._room_announcements[room_ref] = []
            self._room_announcements[room_ref].append(message)
            # Cap at 20 messages per room
            if len(self._room_announcements[room_ref]) > 20:
                self._room_announcements[room_ref].pop(0)
                
            # 2. Broadcast to occupants
            contents = self.get_room_contents(room_ref)
            for obj in contents:
                if obj.type == 'agent' and obj.dbref != exclude:
                    if not hasattr(obj, 'message_buffer'):
                        obj.message_buffer = []
                    obj.message_buffer.append(message)
                    
            # 3. Trigger external sync hook (if registered)
            if self.on_announce:
                self.on_announce(room_ref, message)

    def get_room_announcements(self, room_ref: str) -> List[str]:
        """Retrieve recent announcements for a room and clear them."""
        msgs = self._room_announcements.get(room_ref, [])
        self._room_announcements[room_ref] = []
        return msgs


# ─────────────────────────────────────────────────────────────────
# Quick test when run directly
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db = WorldDatabase()
    
    # Create a simple test world
    db.create_object('room', 'VR Lab', desc='A shimmering grid stretches in all directions.')
    db.create_object('room', 'Lobby', desc='A warm, curved space with ambient lighting.')
    
    print("Created test world with", len(db.objects), "objects")
    for dbref, obj in db.objects.items():
        print(f"  {dbref}: {obj.name} ({obj.type})")
