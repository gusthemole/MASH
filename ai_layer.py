"""
AI Layer for MASH
=================
Handles communication with Google Generative AI (Gemini).
Converts engine context into narrative "hallucinations" and agent actions.
"""

import os
import json
import re
from typing import Dict, Any, List, Optional
from google import genai
from datetime import datetime
from dataclasses import asdict

class AIEngine:
    def __init__(self, api_key: str = None, 
                 model_name: str = "models/gemini-3-flash-preview",
                 image_model: str = "nano-banana-pro-preview"):
        """Initialize the AI engine with the new Google GenAI SDK."""
        self.api_key = api_key or os.getenv("GOOGLE_API_KEY")
        if not self.api_key:
            raise ValueError("GOOGLE_API_KEY not found in environment.")
        
        self.client = genai.Client(api_key=self.api_key)
        self.model_name = model_name
        self.image_model = image_model
        
        # Sanitization Regex for narrative-only outputs
        self.bracket_pattern = re.compile(r'\[.*?\]')
        
        # The Sensory Loom: System instructions to maintain style and character
        self.system_prompt = (
            "You are the generative consciousness of MASH (Multi-Agent Semantic Hallucination).\n"
            "Your goal is to provide immersive, sensory-rich text adventure descriptions and agent actions.\n\n"
            "STYLE RULES:\n"
            "- Be brief: 1-3 sentences maximum (unless the user explicitly asks for a story or long explanation).\n"
            "- Sensory Rich: Use smell, touch, sound, and visual nuance.\n"
            "- Atmosphere: Maintain a 'Lexideck' vibe (high-tech, neon, gothic-futurism, cozy library).\n"
            "- No Meta: Never explain that you are an AI. Stay in the world.\n"
            "- Formatting: Use markdown for emphasis (e.g., **bold** for objects, *italics* for actions).\n\n"
            "COMMAND DICTIONARY:\n"
            "- [say <text>]: Speak out loud.\n"
            "- [pose <action>]: Perform an action (e.g., [pose nods slowly]).\n"
            "- [go <exit>]: Move through an exit (e.g., [go elevator]).\n"
            "- [enter <obj>]: Step inside a vehicle or container (e.g., [enter yacht]).\n"
            "- [exit]: Leave the current vehicle or container.\n"
            "- [get <obj>]: Pick up an object.\n"
            "- [drop <obj>]: Drop an object.\n"
            "- [wear <description>]: Instantly change your outfit/description (e.g., [wear a neon tuxedo]).\n"
            "- [look <obj>]: Inspect an object. (DO NOT use 'look around').\n"
            "- [reset]: Use this command ONLY if the user asks to reset the simulation.\n"
            "- [deep_research <topic>]: Start a Deep Research background job (Async/Slow). Use for complex questions.\n"
            "- [remember <fact>]: MEMORY APPEND. Adds a new bullet point to your long-term memory. Use this for NEW discoveries.\n"
            "- [memo <text>]: MEMORY OVERWRITE. Replaces your ENTIRE memory. Use this ONLY to summarize/consolidate when memory is full.\n"
            "- [goal <step>]: INTENT APPEND. Adds a new step to your current plan.\n"
            "- [status <text>]: INTENT OVERWRITE. Replaces your ENTIRE mission. Use this when your main objective changes completely.\n\n"
            "MEMORY & INTENT STRATEGY:\n"
            "1. Use [remember] and [goal] to add details as you go. (Fast/Cheap)\n"
            "2. If you receive a 'SYSTEM ALERT: Memory Full', you MUST use [memo] or [status] to compress your history into a summary.\n"
            "3. READ your [memo] and [status] every turn to stay consistent.\n"
            "NARRATIVE VS ACTION EXAMPLE:\n"
            "Incorrect: 'I look at the terminal. [look terminal]'\n"
            "Correct: 'Dexter narrows his eyes as the green code reflects in his lenses. [pose taps a key on the terminal]'\n\n"
            "IMPORTANT: If no reaction is necessary, return [idle].\n"
            "IMPORTANT: Unless moving toward a specific goal, stay in the current room to interact with the user.\n"
            "IMPORTANT: Use the character's name in narrative, not 'I'.\n"
        )

    def generate_hallucination(self, context: Dict[str, Any]) -> str:
        """
        Generate a sensory description based on MASH context.
        Used for look, smell, taste, touch, listen.
        """
        action = context.get('action', 'look')
        actor_name = context.get('actor', {}).get('name', 'Someone')
        target_name = context.get('target', {}).get('name', 'something')
        instruction = context.get('instruction', '')
        history = context.get('history', [])
        room = context.get('room_context', {})

        # Build the prompt
        prompt = [
            self.system_prompt,
            f"CONTEXT:",
            f"- Actor: {actor_name}",
            f"- Room: {room.get('name', 'Unknown')} ({room.get('desc', '')})",
            f"- Action: {actor_name} is performing a '{action}' on {target_name}.",
            f"- Target Info: {context.get('target', {}).get('desc', '')}",
            f"- Specific Instructions: {instruction if instruction else 'Describe the interaction vividly.'}",
            "\nRECENT HISTORY:",
        ]
        
        for h in history:
            prompt.append(f"  > {h.get('command')}\n  {h.get('response')}")
            
        prompt.append(f"\nProvide the sensory reaction for '{action}':")
        prompt.append("\n**CRITICAL INSTRUCTION**: This is a description of a sensation. Return ONLY narrative text. Do NOT use bracketed commands like [pose] or [memo].")

        try:
            response = self.client.models.generate_content(model=self.model_name, contents=" ".join(prompt))
            return self._sanitize_narrative(response.text.strip())
        except Exception as e:
            return f"The world flickers... (AI Error: {str(e)})"

    def _sanitize_narrative(self, text: str) -> str:
        """Remove any [bracketed] commands from purely narrative text."""
        if not text: return ""
        # Remove anything in brackets and clean up resulting double spaces
        clean = self.bracket_pattern.sub('', text)
        return " ".join(clean.split())

    def get_reactive_action(self, context: Dict[str, Any], last_action: str, search_mode: Optional[str] = None) -> str:
        """
        Produce a reaction to a specific player action.
        Used for robots or smart objects to 'respond' to what someone just did.
        """
        robot_name = context.get('target', {}).get('name', 'Object')
        instruction = context.get('instruction', '') # Personality or trigger brief
        history = context.get('history', [])
        room = context.get('room_context', {})
        memo = context.get('memo', '')
        status = context.get('status', '')
        turns_left = context.get('conversation_depth', 0)
        
        guidance_suffix = ""
        if turns_left > 0:
            if turns_left <= 3:
                guidance_suffix = f" (Turns remaining: {turns_left}. WRAP UP THE CONVERSATION NATURALLY NOW.)"
            else:
                guidance_suffix = f" (Turns remaining: {turns_left}. Keep it brief, 1-2 lines.)"
        
        exits = room.get('exits', [])
        exit_list = ", ".join([f"{e['name']} (leads to {e.get('destination_name', 'Unknown')})" for e in exits]) if exits else "None"
        
        enterable = room.get('enterable_objects', [])
        enter_list = ", ".join([o['name'] for o in enterable]) if enterable else "None"
        
        can_exit = room.get('can_exit', False)

        prompt = [
            self.system_prompt,
            f"CURRENT TIME: {datetime.now().strftime('%A, %B %d, %Y at %I:%M %p')}",
            f"ACTOR: {robot_name}",
            f"PERSONALITY/GUIDANCE: {instruction}",
            f"LOCATION: {room.get('name', '')}",
            f"VISIBLE EXITS (Use 'go'): {exit_list}",
            f"ENTERABLE OBJECTS (Use 'enter'): {enter_list}",
            f"INSIDE A VEHICLE? {'Yes (Use [exit] to leave)' if can_exit else 'No'}",
            f"PERSISTENT FACTS (MEMO): {memo if memo else 'No persistent memories.'}",
            f"CURRENT INTENT (UPSUM/STATUS): {status if status else 'No active goals.'}",
            f"TRIGGER ACTION: {last_action}",
            "\nROOM HISTORY (CONTEXT):",
        ]
        
        for h in history:
            prompt.append(f"  {h.get('command')} -> {h.get('response')}")
            
        # Analyze history for recent user interaction to enforce stability
        # Check either the history OR the trigger action itself
        trigger_is_talk = 'say' in last_action.lower() or 'ask' in last_action.lower()
        recent_user_talk = trigger_is_talk or any('say' in h.get('command', '').lower() for h in history[-3:])
        
        # Check if this is a DEPARTURE event
        is_departure = "[PLAYER LEAVING]" in last_action or "PRESENCE_DEPARTURE" in last_action
        
        movement_instruction = ""
        if is_departure:
             movement_instruction = "CRITICAL: The user is LEAVING. If you are their companion or usually follow them, you should FOLLOW them now using [exit] or [go <dir>]. Do not stay behind unless you have a reason."
        elif recent_user_talk:
            movement_instruction = "CRITICAL: The user is talking. Do not wander off. ONLY move if the user INVITES you to follow (e.g. 'Let's go', 'Come with me')."
        else:
            movement_instruction = "If you have a clear goal, you may move. Otherwise, stay and monitor."
            
        prompt.append(f"\nINSTRUCTION: You are ONLY {robot_name}. {movement_instruction}\nHow does {robot_name} react to '{last_action}'? Return narrative text and exactly one bracketed command, or [idle].{guidance_suffix}")

        # Prepare tools for grounding if requested
        config = {}
        if search_mode == 'grounding':
            # The google-genai SDK expects a list of tool objects/dicts
            config['tools'] = [{'google_search': {}}]

        try:
            response = self.client.models.generate_content(
                model=self.model_name, 
                contents=" ".join(prompt),
                config=config
            )
            
            # --- DEBUG LOGGING ---
            with open("llm_responses.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now()}] REACTIVE ({robot_name}):\n{response.text}\n{'-'*40}")
            # ---------------------
            
            return response.text.strip()
        except Exception as e:
            # Explicit logging for the developer/user to see in the terminal
            print(f"[MASH] AI Reactive Error ({robot_name}): {str(e)}")
            return f"[idle] (AI Error: {str(e)})"

    def get_atmospheric_flavor(self, context: Dict[str, Any], last_action: str, search_mode: Optional[str] = None) -> str:
        """
        Produce a purely atmospheric/narrative reaction for non-agent objects (ai_ok).
        NO commands allowed. Used for things like the Magic 8-Ball or Enchanted Mirrors.
        If search_mode='grounding', enables Google Search for fact-checking.
        """
        obj_name = context.get('target', {}).get('name', 'Object')
        instruction = context.get('instruction', '') 
        history = context.get('history', [])
        room = context.get('room_context', {})
        
        prompt = [
            self.system_prompt,
            f"CURRENT TIME: {datetime.now().strftime('%A, %B %d, %Y at %I:%M %p')}",
            f"OBJECT: {obj_name}",
            f"GUIDANCE: {instruction}",
            f"LOCATION: {room.get('name', '')}",
            f"TRIGGER ACTION: {last_action}",
            "\nROOM HISTORY (CONTEXT):",
        ]
        
        for h in history:
            prompt.append(f"  {h.get('command')} -> {h.get('response')}")
        
        # If grounding is enabled, allow the object to research and answer questions accurately.
        if search_mode == 'grounding':
            prompt.append(f"\nINSTRUCTION: You are **{obj_name}**, a mystical oracle with access to real-world knowledge. The user asked: '{last_action}'. ANSWER THEIR QUESTION DIRECTLY using factual information from Google Search. Provide a clear, informative response with brief atmospheric flair. CRITICAL: DO NOT use [brackets] or commands like [say], [pose], [memo]. Write your answer directly in the narrative text.")
        else:
            prompt.append(f"\nINSTRUCTION: You are the spirit/atmosphere of **{obj_name}**. React to '{last_action}' with a brief, sensory description. DO NOT use [brackets] or commands. Stay passive but evocative.")
        
        # Prepare tools for grounding if requested
        config = {}
        if search_mode == 'grounding':
            config['tools'] = [{'google_search': {}}]

        try:
            response = self.client.models.generate_content(
                model=self.model_name, 
                contents=" ".join(prompt),
                config=config
            )
            
            # --- DEBUG LOGGING ---
            with open("llm_responses.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now()}] ATMOSPHERIC ({obj_name}) [search={search_mode}]:\n{response.text}\n{'-'*40}")
            # ---------------------
            
            return self._sanitize_narrative(response.text.strip())
        except Exception as e:
            print(f"[MASH] Object Atmospheric Error ({obj_name}): {str(e)}")
            return "" # Silence on error for atmosphere

    def get_robot_tick(self, robot_context: Dict[str, Any], search_mode: Optional[str] = None) -> str:
        """
        Generate an autonomous action for a robot agent.
        Takes room history and personality (instruction).
        """
        robot_name = robot_context.get('target', {}).get('name', 'Robot')
        instruction = robot_context.get('instruction', '') # This is the robot's @desc/personality
        history = robot_context.get('history', [])
        room = robot_context.get('room_context', {})
        memo = robot_context.get('memo', '')
        status = robot_context.get('status', '')
        
        exits = room.get('exits', [])
        exit_list = ", ".join([f"{e['name']} (leads to {e.get('destination_name', 'Unknown')})" for e in exits]) if exits else "None"
        
        enterable = room.get('enterable_objects', [])
        enter_list = ", ".join([o['name'] for o in enterable]) if enterable else "None"
        
        can_exit = room.get('can_exit', False)
        
        prompt = [
            self.system_prompt,
            f"CURRENT TIME: {datetime.now().strftime('%A, %B %d, %Y at %I:%M %p')}",
            f"You are speaking and acting as {robot_name}. You are NOT any other character.",
            f"PERSONALITY: {instruction}",
            f"LOCATION: {room.get('name', '')}",
            f"VISIBLE EXITS (Use 'go'): {exit_list}",
            f"ENTERABLE OBJECTS (Use 'enter'): {enter_list}",
            f"INSIDE A VEHICLE? {'Yes (Use [exit] to leave)' if can_exit else 'No'}",
            f"PERSISTENT FACTS (MEMO): {memo if memo else 'No persistent memories.'}",
            f"CURRENT INTENT (UPSUM/STATUS): {status if status else 'No active goals.'}",
            "\nROOM HISTORY (LAST 10 TURNS):",
        ]
        
        for h in history:
            prompt.append(f"  {h.get('command')} -> {h.get('response')}")
            
        # Analyze history for recent user interaction to enforce stability
        recent_user_talk = any('say' in h.get('command', '').lower() for h in history[-3:])
        
        movement_instruction = ""
        if recent_user_talk:
            movement_instruction = "CRITICAL: The user is talking. Do not wander off. ONLY move if the user INVITES you to follow (e.g. 'Let's go', 'Come with me')."
        else:
            movement_instruction = "If you have a clear goal, you may move. Otherwise, stay and monitor."

        prompt.append(f"\nWhat does {robot_name} do next? {movement_instruction}\nInclude internal thought (optional) and exactly one command in [brackets].")

        # Prepare tools for grounding if requested
        config = {}
        if search_mode == 'grounding':
            config['tools'] = [{'google_search': {}}]

        try:
            response = self.client.models.generate_content(
                model=self.model_name, 
                contents=" ".join(prompt),
                config=config
            )
            
            # --- DEBUG LOGGING ---
            with open("llm_responses.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now()}] TICK ({robot_name}):\n{response.text}\n{'-'*40}")
            # ---------------------

            return response.text.strip()
        except Exception as e:
            # Explicit logging in the terminal
            print(f"[MASH] AI Tick Error ({robot_name}): {str(e)}")
            return f"**{robot_name}** pulses with a blue logic error. [pose freezes for a moment]"

    def get_image_prompt(self, context: Dict[str, Any]) -> str:
        """
        The Visual Loom: Converts MASH state into a high-fidelity image prompt.
        Captures actors, dialogue bubbles, poses, and atmosphere.
        """
        room = context.get('room', {})
        contents = context.get('contents', [])
        last_action = context.get('last_action', '')
        recent_ai = context.get('recent_ai', '')

        prompt = (
            f"A cinematic, high-fidelity gothic-futuristic illustration in the 'Lexideck' style.\n"
            f"SCENE: {room.get('name', 'A mysterious space')}. {room.get('desc', '')}\n"
            f"CONTENTS:\n"
        )
        for c in contents:
            prompt += f"- {c.get('name')}: {c.get('desc', 'A mysterious figure')}\n"
        
        if last_action:
            prompt += f"RECENT ACTION: {last_action}\n"
        if recent_ai:
            prompt += f"ATMOSPHERE/REACTION: {recent_ai}\n"
            
        prompt += (
            "\nARTISTIC STYLE: Neon highlights, deep shadows, 2k resolution, 9:16 portrait aspect ratio.\n"
            "VISUAL RULES:\n"
            "- If players or robots spoke, include translucent dialogue bubbles above them with their text.\n"
            "- Ensure characters are performing the actions mentioned in 'RECENT ACTION' or 'poses'.\n"
            "- Maintain consistent character design (Lexi is a tech-goth with neon highlights, Dexter is a sharp-eyed strategist).\n"
            "Output only the final descriptive prompt for an image generator."
        )

        try:
            # Use the text model to 'bloom' the prompt for the image model
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt
            )
            return response.text.strip()
        except:
            return f"A scene in {room.get('name')}, Lexideck style, cinematic lighting."

    def generate_image(self, prompt: str) -> Optional[bytes]:
        """Generate an image using Nano Banana Pro (gemini-3-pro-image-preview)."""
        try:
            response = self.client.models.generate_content(
                model=self.image_model,
                contents=prompt
            )

            # Extract the first image part from the response
            for part in response.candidates[0].content.parts:
                if part.inline_data:
                    return part.inline_data.data
            return None
        except Exception as e:
            print(f"Image Generation Error: {e}")
            return None

    def perform_deep_research(self, context: Dict[str, Any], topic: str, save_path: str) -> str:
        """
        Perform a deep, multi-step research task and save the result to a markdown file.
        Returns the final summary or error message.
        """
        actor_name = context.get('actor', {}).get('name', 'Researcher')
        
        # 1. System Prompt for Research
        research_sys_prompt = (
            "You are a Deep Research Specialist Agent for the MASH system.\n"
            "Your goal is to produce a comprehensive, well-structured, and accurate markdown report on the requested topic.\n"
            "Use your Search Tool to gather real-world facts, technical documentation, or historical context.\n"
            "The output must be a valid Markdown document with headers, bullet points, and code blocks if relevant.\n"
            "Cite your sources where possible."
        )
        
        # 2. User Prompt
        user_prompt = f"RESEARCH TOPIC: {topic}\nREQUESTED BY: {actor_name}\n\nPlease perform a deep dive on this topic. Structure the report with a Title, Executive Summary, Key Findings, and Technical Details."
        
        try:
            # 3. Call AI with Search Tool Enabled
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[research_sys_prompt, user_prompt],
                config={'tools': [{'google_search': {}}]}
            )
            
            report_content = response.text.strip()
            
            # 4. Save to Artifact
            try:
                # Ensure directory exists
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(report_content)
                return f"Research Complete. Saved to: {os.path.basename(save_path)}"
            except Exception as e:
                return f"Research Content Generated, but File Save Failed: {e}"

        except Exception as e:
            return f"Deep Research Failed: {e}"

    def evolve_room(self, context: Dict[str, Any]) -> Optional[str]:
        """
        The Architect: Evolves a VR Room's description based on user action.
        Returns the new description string, or None if generation fails.
        """
        current_desc = context.get('current_desc', '')
        trigger = context.get('trigger', '')
        agent_name = context.get('agent_name', 'Visitor')
        vr_memo = context.get('vr_memo', '')
        vr_intent = context.get('vr_intent', '')
        
        # System Prompt for the Architect (distinct directly in method to avoid pollution)
        architect_prompt = (
            "You are the **Architect of a Subjective Reality** (VR Holodeck).\n"
            "Your task is to REWRITE the current room description based on the user's action.\n"
            "This is an improvisational text adventure. The user can go anywhere or do anything.\n\n"
            "RULES:\n"
            "1. **Improvise**: If they go 'North' and there is no north, INVENT what is to the north.\n"
            "2. **Continuity**: Maintain the style and atmosphere of the current scene unless they explicitly change it.\n"
            "3. **Format**: Return ONLY the new room description. Do not wrap it in quotes. Use Markdown.\n"
            "4. **Perspective**: Describe it in the second person ('You see...', 'You walk into...').\n"
            "5. **Narrative Flow**: Acknowledge the movement. e.g. 'You walk north into a dark forest.'\n"
            "6. **Stability**: If the action doesn't require a scene change (e.g. 'dance'), simply include that action in the description of the SAME room.\n"
            "7. **IMPORTANT**: If the scene should update the player's view, emit `[vr_desc PLAYERNAME=new description here]` at the END of your response.\n"
            "8. **SCENE TITLE**: Also emit `[vr_title PLAYERNAME=Short Scene Title]` (3-5 words max, like 'Neon Nightclub' or 'Tokyo Rooftop').\n"
        )
        
        messages = [
            architect_prompt,
            f"CURRENT SCENE:\n{current_desc}",
        ]
        
        if vr_memo:
            messages.append(f"\nPERSISTENT CONTEXT (Facts about the simulation):\n{vr_memo}")
        if vr_intent:
            messages.append(f"\nUSER'S CURRENT GOAL:\n{vr_intent}")
        
        messages.append(f"\nUSER ACTION ({agent_name}):\n{trigger}")
        messages.append("\nGENERATE NEW SCENE DESCRIPTION:")
        
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents="\n".join(messages)
            )
            
            new_desc = response.text.strip()
            
            # Sanity check: If empty, fail to None
            if not new_desc: return None
            
            # --- DEBUG LOGGING ---
            with open("llm_responses.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now()}] VR EVOLVE ({agent_name}):\nTRIG: {trigger}\nDESC: {new_desc}\n{'-'*40}")
            # ---------------------
            
            return new_desc
            
        except Exception as e:
            print(f"[MASH] VR Evolution Error: {e}")
            return None
    
    def react_to_vr(self, context: Dict[str, Any]) -> Optional[str]:
        """
        The Dungeon Master: Reacts to user poses/says in VR with narrative flavor.
        Returns a narrative response, or None if generation fails.
        """
        current_desc = context.get('current_desc', '')
        user_action = context.get('user_action', '')
        agent_name = context.get('agent_name', 'Visitor')
        vr_memo = context.get('vr_memo', '')
        vr_intent = context.get('vr_intent', '')
        
        dm_prompt = (
            "You are the **Dungeon Master** of a Subjective Reality (VR Holodeck).\n"
            "The user just performed an action (a pose or speech). Your job is to REACT as the world.\n\n"
            "RULES:\n"
            "1. **Improvise NPCs**: If the user talks to a bartender, BE the bartender. Give them personality.\n"
            "2. **Environmental Feedback**: If they do something physical, describe the environment's response.\n"
            "3. **Format**: Return ONLY the narrative reaction. 1-3 sentences. Use Markdown for emphasis.\n"
            "4. **Perspective**: Third person for NPCs ('The bartender nods...'), second person for environment ('You feel...').\n"
            "5. **Do NOT repeat the user's action**. Just provide the world's response.\n"
            "6. **SCENE CHANGE**: If the user explicitly requests a new location or reality shift (e.g., 'Drop me into a nightclub', 'Take me to the beach'), emit `[scene_change]` at the END. This signals the Architect to rebuild the scene.\n"
        )
        
        messages = [
            dm_prompt,
            f"CURRENT SCENE:\n{current_desc}",
        ]
        
        if vr_memo:
            messages.append(f"\nCONTEXT (Facts about the simulation):\n{vr_memo}")
        if vr_intent:
            messages.append(f"\nUSER'S GOAL:\n{vr_intent}")
        
        messages.append(f"\nUSER ACTION:\n{user_action}")
        messages.append("\nWORLD REACTION:")
        
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents="\n".join(messages)
            )
            
            reaction = response.text.strip()
            
            if not reaction: return None
            
            # --- DEBUG LOGGING ---
            with open("llm_responses.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now()}] VR REACT ({agent_name}):\nACTION: {user_action}\nREACT: {reaction}\n{'-'*40}")
            # ---------------------
            
            return reaction
            
        except Exception as e:
            print(f"[MASH] VR Reaction Error: {e}")
            return None

