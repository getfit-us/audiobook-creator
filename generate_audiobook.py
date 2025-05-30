"""
Audiobook Creator
Copyright (C) 2025 Prakhar Sharma

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import shutil
from openai import AsyncOpenAI
from tqdm import tqdm
import json
import os
import asyncio
import re
from word2number import w2n
import time
import sys
from config.constants import (
    API_KEY,
    BASE_URL,
    MODEL,
    MAX_PARALLEL_REQUESTS_BATCH_SIZE,
    TEMP_DIR,
)
from utils.check_tts_api import check_tts_api
from utils.run_shell_commands import (
    check_if_ffmpeg_is_installed,
    check_if_calibre_is_installed,
)
from utils.file_utils import concatenate_audio_files, read_json, empty_directory
from utils.audiobook_utils import (
    add_silence_to_audio_file_by_appending_silence_file,
    merge_chapters_to_m4b,
    convert_audio_file_formats,
    merge_chapters_to_standard_audio_file,
    add_silence_to_audio_file_by_appending_pre_generated_silence,
)
from utils.check_tts_api import check_tts_api
from dotenv import load_dotenv
import subprocess
import random
from utils.task_utils import (
    update_task_status,
    is_task_cancelled,
    get_task_progress_index,
    set_task_progress_index,
)
from utils.tts_api import generate_tts_with_retry, select_tts_voice

load_dotenv()


API_OUTPUT_FORMAT = "wav" if MODEL == "orpheus" else "aac"

os.makedirs("audio_samples", exist_ok=True)

async_openai_client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)

print(BASE_URL)


def sanitize_filename(text):
    # Remove or replace problematic characters
    text = text.replace("'", "").replace('"', "").replace("/", " ").replace(".", " ")
    text = text.replace(":", "").replace("?", "").replace("\\", "").replace("|", "")
    text = text.replace("*", "").replace("<", "").replace(">", "").replace("&", "and")

    # Normalize whitespace and trim
    text = " ".join(text.split())

    return text


def sanitize_book_title_for_filename(book_title):
    """Sanitize book title to be safe for filesystem use"""
    safe_title = "".join(
        c for c in book_title if c.isalnum() or c in (" ", "-", "_")
    ).rstrip()
    return safe_title or "audiobook"  # fallback if title becomes empty


def split_and_annotate_text(text):
    """Splits text into dialogue and narration while annotating each segment."""
    parts = re.split(r'("[^"]+")', text)  # Keep dialogues in the split result
    annotated_parts = []

    for part in parts:
        if part:  # Ignore empty strings
            annotated_parts.append(
                {
                    "text": part,
                    "type": (
                        "dialogue"
                        if part.startswith('"') and part.endswith('"')
                        else "narration"
                    ),
                }
            )

    return annotated_parts


def check_if_chapter_heading(text):
    """
    Checks if a given text line represents a chapter heading.

    A chapter heading is considered a string that starts with either "Chapter",
    "Part", or "PART" (case-insensitive) followed by a number (either a digit
    or a word that can be converted to an integer).

    :param text: The text to check
    :return: True if the text is a chapter heading, False otherwise
    """
    pattern = r"^(Chapter|Part|PART)\s+([\w-]+|\d+)"
    regex = re.compile(pattern, re.IGNORECASE)
    match = regex.match(text)

    if match:
        label, number = match.groups()
        try:
            # Try converting the number (either digit or word) to an integer
            w2n.word_to_num(number) if not number.isdigit() else int(number)
            return True
        except ValueError:
            return False  # Invalid number format
    return False  # No match


def concatenate_chapters(
    chapter_files, book_title, chapter_line_map, temp_line_audio_dir
):
    """
    Concatenates the chapters into a single audiobook file.
    """
    # Third pass: Concatenate audio files for each chapter in order
    chapter_assembly_bar = tqdm(
        total=len(chapter_files), unit="chapter", desc="Assembling Chapters"
    )

    def assemble_single_chapter(chapter_file):
        # Create a temporary file list for this chapter's lines
        chapter_lines_list = os.path.join(
            f"{TEMP_DIR}/{book_title}",
            f"chapter_lines_list_{chapter_file.replace('/', '_').replace('.', '_')}.txt",
        )

        # Delete the chapter_lines_list file if it exists
        if os.path.exists(chapter_lines_list):
            os.remove(chapter_lines_list)

        with open(chapter_lines_list, "w", encoding="utf-8") as f:
            for line_index in sorted(chapter_line_map[chapter_file]):
                line_audio_path = os.path.join(
                    temp_line_audio_dir, f"line_{line_index:06d}.{API_OUTPUT_FORMAT}"
                )
                # Use absolute path to prevent path duplication issues
                f.write(f"file '{os.path.abspath(line_audio_path)}'\n")

        # Use FFmpeg to concatenate the lines with optimized parameters
        if MODEL == "orpheus":
            # For Orpheus, convert WAV segments to M4A chapters directly with timestamp filtering
            ffmpeg_cmd = (
                f'ffmpeg -y -f concat -safe 0 -i "{chapter_lines_list}" '
                f'-c:a aac -b:a 256k -ar 44100 -ac 2 -avoid_negative_ts make_zero -fflags +genpts -threads 0 "{TEMP_DIR}/{book_title}/{chapter_file}"'
            )
        else:
            # For other models, use re-encoding with timestamp filtering to prevent truncation
            ffmpeg_cmd = f'ffmpeg -y -f concat -safe 0 -i "{chapter_lines_list}" -c:a aac -b:a 256k -avoid_negative_ts make_zero -fflags +genpts -threads 0 "{TEMP_DIR}/{book_title}/{chapter_file}"'

        try:
            result = subprocess.run(
                ffmpeg_cmd, shell=True, check=True, capture_output=True, text=True
            )
            print(f"[DEBUG] FFmpeg stdout: {result.stdout}")
            print(f"[DEBUG] FFmpeg stderr: {result.stderr}")
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] FFmpeg failed for {chapter_file}:")
            print(f"[ERROR] Command: {ffmpeg_cmd}")
            print(f"[ERROR] Stdout: {e.stdout}")
            print(f"[ERROR] Stderr: {e.stderr}")
            raise e

        print(f"Assembled chapter: {chapter_file}")

        # Clean up the temporary file list
        os.remove(chapter_lines_list)
        return chapter_file

    # Process chapters in parallel (limit to 4 concurrent to avoid overwhelming system)
    import concurrent.futures

    max_workers = min(4, len(chapter_files))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(assemble_single_chapter, chapter_file)
            for chapter_file in chapter_files
        ]

        for future in concurrent.futures.as_completed(futures):
            try:
                chapter_file = future.result()
                chapter_assembly_bar.update(1)
            except Exception as e:
                print(f"Error assembling chapter: {e}")
                raise e

    chapter_assembly_bar.close()


async def parallel_post_processing(chapter_files, book_title, output_format):
    """
    Parallel post-processing of chapter files to add silence and convert formats.
    """

    print(
        f"chapter_files: {chapter_files}, book_title: {book_title}, output_format: {output_format}"
    )

    def process_single_chapter(chapter_file):
        # Add silence to chapter file

        # Convert to M4A format if needed
        chapter_name = chapter_file.split(f".{output_format}")[0]
        m4a_chapter_file = f"{chapter_name}.m4a"

        # Only convert if not already in M4A format
        if not chapter_file.endswith(".m4a"):
            convert_audio_file_formats(
                output_format, "m4a", f"{TEMP_DIR}/{book_title}", chapter_name
            )
        else:
            m4a_chapter_file = chapter_file

        add_silence_to_audio_file_by_appending_silence_file(m4a_chapter_file)

        return m4a_chapter_file

    # Process chapters in parallel (limit to 4 concurrent to avoid overwhelming system)
    import concurrent.futures

    max_workers = min(4, len(chapter_files))
    # Initialize list to store results in the correct order
    m4a_chapter_files = [None] * len(chapter_files)

    post_processing_bar = tqdm(
        total=len(chapter_files), unit="chapter", desc="Post Processing (Parallel)"
    )

    if not chapter_files:  # Handle empty list gracefully
        post_processing_bar.close()
        return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Map futures to their original index to ensure order is preserved
        future_to_index = {
            executor.submit(process_single_chapter, chapter_files[i]): i
            for i in range(len(chapter_files))
        }

        for future in concurrent.futures.as_completed(future_to_index):
            original_index = future_to_index[future]
            try:
                processed_m4a_file = future.result()
                m4a_chapter_files[original_index] = processed_m4a_file
                post_processing_bar.update(1)
            except Exception as e:
                # Log the specific chapter that failed if possible
                failed_chapter_name = "unknown"
                if original_index < len(chapter_files):
                    failed_chapter_name = chapter_files[original_index]
                print(
                    f"Error in post-processing for chapter {failed_chapter_name}: {e}"
                )
                post_processing_bar.close()  # Ensure bar is closed on error
                raise e

    post_processing_bar.close()

    # The m4a_chapter_files list is now populated in the correct order,
    # so sorting is no longer needed.
    # m4a_chapter_files.sort() # This line is removed
    return m4a_chapter_files


def find_voice_for_gender_score(character: str, character_gender_map, voice_map):
    """
    Finds the appropriate voice for a character based on their gender score.

    This function takes in the name of a character, a dictionary mapping character names to their gender scores,
    and a dictionary mapping voice identifiers to gender scores. It returns the voice identifier that matches the
    character's gender score.

    Args:
        character (str): The name of the character for whom the voice is being determined.
        character_gender_map (dict): A dictionary mapping character names to their gender scores.
        voice_map (dict): A dictionary mapping voice identifiers to gender scores.

    Returns:
        str: The voice identifier that matches the character's gender score.
    """

    try:
        # Get the character's gender score
        character_lower = character.lower()

        # Check if character exists in the character_gender_map
        if character_lower not in character_gender_map["scores"]:
            print(
                f"WARNING: Character '{character}' not found in character_gender_map. Using narrator voice as fallback."
            )
            # Use narrator's voice as fallback
            if "narrator" in character_gender_map["scores"]:
                character_gender_score_doc = character_gender_map["scores"]["narrator"]
                character_gender_score = character_gender_score_doc["gender_score"]
            else:
                print(
                    f"ERROR: Even narrator not found in character_gender_map. Using score 5 (neutral)."
                )
                character_gender_score = 5
        else:
            character_gender_score_doc = character_gender_map["scores"][character_lower]
            character_gender_score = character_gender_score_doc["gender_score"]

        # Iterate over the voice identifiers and their scores
        for voice, score in voice_map.items():
            # Find the voice identifier that matches the character's gender score
            if score == character_gender_score:
                return voice

        # If no exact match found, find the closest gender score
        print(
            f"WARNING: No exact voice match for character '{character}' with gender score {character_gender_score}. Finding closest match."
        )

        closest_voice = None
        closest_diff = float("inf")

        for voice, score in voice_map.items():
            diff = abs(score - character_gender_score)
            if diff < closest_diff:
                closest_diff = diff
                closest_voice = voice

        if closest_voice:
            print(
                f"Using voice '{closest_voice}' (score {voice_map[closest_voice]}) for character '{character}' (score {character_gender_score})"
            )
            return closest_voice

        # Final fallback: use the first available voice
        if voice_map:
            fallback_voice = list(voice_map.keys())[0]
            print(
                f"ERROR: Could not find suitable voice for character '{character}'. Using fallback voice '{fallback_voice}'."
            )
            return fallback_voice

        # Absolute fallback for empty voice_map
        print(f"CRITICAL ERROR: voice_map is empty. Using hardcoded fallback voice.")
        return "af_heart"  # Default fallback voice

    except Exception as e:
        print(f"ERROR in find_voice_for_gender_score for character '{character}': {e}")
        print("Using hardcoded fallback voice.")
        return "af_heart"  # Default fallback voice


def validate_and_clean_text_for_tts(text):
    """
    Validate and clean text before sending to TTS API.
    Returns cleaned text or None if text should be skipped.
    """
    if not text:
        return None

    text = text.strip()
    if not text:
        return None

    # Check for minimum meaningful content
    if len(text) < 1:
        return None

    # Check if text has any speakable content (letters or numbers)
    import re

    if not re.search(r"[a-zA-Z0-9]", text):
        # Only punctuation/symbols, might cause TTS issues
        print(f"WARNING: Text contains no alphanumeric characters: '{text}'")
        # Return None to skip this part, or add minimal content
        if len(text) <= 5:  # Very short punctuation-only text
            return None
        else:
            # Add minimal speakable content for longer punctuation
            return f"Pause. {text}"

    # Remove excessive whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # Ensure minimum length for TTS
    if len(text) < 2:
        text = f"{text}."

    return text


def preprocess_text_for_orpheus(text):
    """
    Preprocess text for Orpheus TTS to prevent repetition issues.
    Adds full stops where necessary while handling edge cases.
    """
    if not text or len(text.strip()) == 0:
        return text

    text = text.strip()

    # Don't modify very short text (single words or very short phrases)
    if len(text) <= 3:
        return text

    # Check if text already ends with proper punctuation
    punctuation_marks = {".", "!", "?", ":", ";", ",", '"', "'", ")", "]", "}"}
    if text[-1] in punctuation_marks:
        return text

    # Handle dialogue - don't add period inside quotes
    if text.startswith('"') and text.endswith('"'):
        # For dialogue, check if there's already punctuation before the closing quote
        if len(text) > 2 and text[-2] in {".", "!", "?", ",", ";", ":"}:
            return text
        else:
            # Add period before closing quote
            return text[:-1] + '."'

    # Handle text that ends with quotes but doesn't start with them
    if text.endswith('"') and not text.startswith('"'):
        # Check if there's punctuation before the quote
        if len(text) > 1 and text[-2] in {".", "!", "?", ",", ";", ":"}:
            return text
        else:
            # Add period before the quote
            return text[:-1] + '."'

    # For regular narration text, add a period
    return text + "."


async def generate_audio_files(
    output_format,
    narrator_gender,
    generate_m4b_audiobook_file=False,
    book_path="",
    book_title="audiobook",
    type="single_voice",
    task_id=None,
):
    # Read the text from the file
    """
    Generate an audiobook using a single voice for narration and dialogues or multiple voices for multi-voice lines.

    This asynchronous function reads text from a file, processes each line to determine
    if it is narration or dialogue, and generates corresponding audio using specified
    voices. The generated audio is organized by chapters, with options to create
    an M4B audiobook file or a standard audio file in the specified output format.

    Args:
        output_format (str): The desired output format for the final audiobook (e.g., "mp3", "wav").
        narrator_gender (str): The gender of the narrator ("male" or "female") to select appropriate voices.
        generate_m4b_audiobook_file (bool, optional): Flag to determine whether to generate an M4B file. Defaults to False.
        book_path (str, optional): The file path for the book to be used in M4B creation. Defaults to an empty string.

    Yields:
        str: Progress updates as the audiobook generation progresses through loading text, generating audio,
             organizing by chapters, assembling chapters, and post-processing steps.
    """

    # Check if converted book exists, if not, process the book first
    converted_book_path = f"{TEMP_DIR}/{book_title}/converted_book.txt"
    if not os.path.exists(converted_book_path):
        yield "Converting book to text format..."
        # Import and use the book processing function
        from book_to_txt import process_book_and_extract_text

        # Create the temp directory structure
        os.makedirs(f"{TEMP_DIR}/{book_title}", exist_ok=True)

        # Process the book and extract text
        for text in process_book_and_extract_text(book_path, "textract", book_title):
            pass  # The function saves the file automatically
        yield "Book conversion completed"

    with open(converted_book_path, "r", encoding="utf-8") as f:
        text = f.read()
    single_voice_lines = text.split("\n")
    # Filter out empty lines
    single_voice_lines = [line.strip() for line in single_voice_lines if line.strip()]

    # Initialize json_data_array here before it's potentially used
    json_data_array = []
    lines_to_process = []  # This will hold the actual lines/data for processing

    # Setup for multi-voice lines
    if type.lower() == "multi_voice":
        print(f"Processing multi-voice lines")
        # Construct file paths within the book's temp directory
        speaker_file_path = os.path.join(
            TEMP_DIR, book_title, "speaker_attributed_book.jsonl"
        )
        character_map_file_path = os.path.join(
            TEMP_DIR, book_title, "character_gender_map.json"
        )

        # Check if the JSONL file exists
        if not os.path.exists(speaker_file_path):
            yield f"Error: {speaker_file_path} not found. Please run identify_characters_and_output_book_to_jsonl.py first to generate speaker-attributed lines."
            return

        # Check if the character map JSON file exists
        if not os.path.exists(character_map_file_path):
            yield f"Error: {character_map_file_path} not found. Please run identify_characters_and_output_book_to_jsonl.py first."
            return

        with open(speaker_file_path, "r", encoding="utf-8") as file:
            for line in file:
                # Parse each line as a JSON object
                json_object = json.loads(line.strip())
                # Append the parsed JSON object to the array
                json_data_array.append(json_object)

            yield "Loaded speaker-attributed lines from JSONL file"

        # Load mappings for character gender and voice selection
        character_gender_map = read_json(character_map_file_path)
        print(f"Character gender map: {character_gender_map}")
        voice_map = None
        total_lines = len(
            json_data_array
        )  # total_lines is based on json_data_array for multi-voice
        lines_to_process = json_data_array
        print(f"Lines to process: {lines_to_process}")
        print(f"JSON data array: {json_data_array}")

        if narrator_gender == "male":
            if MODEL == "kokoro":
                voice_map = read_json(
                    "static_files/kokoro_voice_map_male_narrator.json"
                )
            else:
                voice_map = read_json(
                    "static_files/orpheus_voice_map_male_narrator.json"
                )
        else:
            if MODEL == "kokoro":
                voice_map = read_json(
                    "static_files/kokoro_voice_map_female_narrator.json"
                )
            else:
                voice_map = read_json(
                    "static_files/orpheus_voice_map_female_narrator.json"
                )

        narrator_voice = find_voice_for_gender_score(
            "narrator", character_gender_map, voice_map
        )
        yield "Loaded voice mappings and selected narrator voice"

    else:
        # Set the voices to be used
        narrator_voice = ""  # voice to be used for narration
        dialogue_voice = ""  # voice to be used for dialogue
        total_lines = len(
            single_voice_lines
        )  # total_lines is based on single_voice_lines for single-voice
        lines_to_process = single_voice_lines

        if narrator_gender == "male":
            if MODEL == "kokoro":
                narrator_voice = "am_puck"
                dialogue_voice = "af_alloy"
            else:
                narrator_voice = "leo"
                dialogue_voice = "dan"
        else:
            if MODEL == "kokoro":
                narrator_voice = "af_heart"
                dialogue_voice = "af_sky"
            else:
                narrator_voice = "tara"
                dialogue_voice = "leah"

    # Setup directories
    temp_line_audio_dir = os.path.join(TEMP_DIR, book_title, "line_segments")

    # if the directory exists we may resume from the last line or use the files to create a different format of the audiobook (e.g. mp3) saving time from re-generating the audio files
    if os.path.exists(temp_line_audio_dir):
        resume_index, _ = get_task_progress_index(task_id)
        print(f"Resuming from line {resume_index}")
    else:
        os.makedirs(TEMP_DIR, exist_ok=True)
        empty_directory(os.path.join(temp_line_audio_dir, book_title))
        os.makedirs(temp_line_audio_dir, exist_ok=True)

    # Batch processing parameters
    semaphore = asyncio.Semaphore(MAX_PARALLEL_REQUESTS_BATCH_SIZE)
    print(f"Starting task {task_id}")

    # Initial setup for chapters
    chapter_index = 1

    if MODEL == "orpheus":
        current_chapter_audio = "Introduction.m4a"
    else:
        current_chapter_audio = f"Introduction.{output_format}"
    chapter_files = []

    resume_index = 0
    if task_id:
        resume_index, _ = get_task_progress_index(task_id)
        print(f"Resuming from line {resume_index}")

    progress_counter = 0
    progress_lock = asyncio.Lock()  # Add lock for progress counter synchronization

    # For tracking progress with tqdm in an async context
    progress_bar = tqdm(
        total=total_lines, unit="line", desc="Audio Generation Progress"
    )

    # Maps chapters to their line indices
    chapter_line_map = {}

    async def update_progress_and_task_status(
        line_index,
        actual_text_content,
    ):
        nonlocal progress_counter
        async with progress_lock:
            progress_bar.update(1)
            progress_counter = progress_counter + 1
            update_task_status(
                task_id,
                "generating",
                f"Generating audiobook. Progress: {progress_counter}/{total_lines}",
            )
            set_task_progress_index(task_id, progress_counter, total_lines)

            return {
                "index": line_index,
                "is_chapter_heading": check_if_chapter_heading(actual_text_content),
                "line": actual_text_content,  # Return the processed text content
            }

    async def process_single_line(
        line_index,
        line,  # 'line' is a dict for multi-voice, str for single-voice
        type="single_voice",
    ):
        async with semaphore:
            nonlocal progress_counter

            actual_text_content = ""
            voice_for_this_line_multi_voice = (
                None  # Store the determined voice for multi-voice lines
            )

            if type.lower() == "multi_voice":
                # 'line' is expected to be a dictionary: {"line": "text", "speaker": "X"}
                if isinstance(line, dict):
                    actual_text_content = line.get("line", "").strip()
                    speaker_name = line.get("speaker", "").strip()

                    if not speaker_name:  # Speaker name is crucial for multi-voice
                        print(
                            f"Warning: Missing speaker name in multi-voice data at index {line_index}: {line}"
                        )
                        # Decide how to handle, e.g., skip or use a default narrator voice. For now, it might fail in find_voice_for_gender_score or use narrator.
                        # If actual_text_content is also empty, it will be caught by the check below.

                    # Only find voice if speaker_name is present
                    if speaker_name:
                        voice_for_this_line_multi_voice = find_voice_for_gender_score(
                            speaker_name, character_gender_map, voice_map
                        )
                        print(
                            f"Speaker: {speaker_name}, Voice: {voice_for_this_line_multi_voice}"
                        )
                    else:  # Fallback if speaker_name is missing, could use narrator or a default
                        print(
                            f"Warning: Using narrator voice for line index {line_index} due to missing speaker."
                        )
                        voice_for_this_line_multi_voice = (
                            narrator_voice  # Or handle as an error
                        )

                    print(f"Line: {actual_text_content}")
                else:
                    print(
                        f"ERROR: Expected a dictionary for multi-voice line at index {line_index}, but got {type(line)}. Content: {line}"
                    )
                    # Fallback to treating 'line' as string, or skip
                    actual_text_content = str(line).strip()
                    # No specific speaker voice can be determined here, will use general dialogue/narrator voice if it proceeds
            else:  # single_voice
                # 'line' is a string
                actual_text_content = str(line).strip()  # Ensure it's a string

            if not actual_text_content:
                # It's important to update progress even for skipped lines if they are part of total_lines
                # However, the original code didn't explicitly update progress here.
                # Assuming empty lines don't contribute to audio parts.
                return None

            annotated_parts = split_and_annotate_text(actual_text_content)
            audio_parts = []
            line_audio_path = os.path.join(
                temp_line_audio_dir, f"line_{line_index:06d}.{API_OUTPUT_FORMAT}"
            )
            # if the line audio file exists and is not empty, we can skip the line
            if (
                os.path.exists(line_audio_path)
                and os.path.getsize(line_audio_path) > 1024
            ):
                return await update_progress_and_task_status(
                    line_index, actual_text_content
                )

            try:
                for i, part in enumerate(annotated_parts):
                    text_to_speak = part["text"].strip()

                    if task_id and is_task_cancelled(task_id):
                        print(
                            f"[DEBUG] Task {task_id} cancelled before processing line {line_index}, part {i}"
                        )
                        raise asyncio.CancelledError("Task was cancelled by user")

                    text_to_speak = validate_and_clean_text_for_tts(text_to_speak)
                    if text_to_speak is None:
                        print(
                            f"Skipping invalid text part {i} in line {line_index} ('{actual_text_content[:50]}...')"
                        )
                        continue

                    if MODEL == "orpheus":
                        text_to_speak = preprocess_text_for_orpheus(text_to_speak)

                    if not text_to_speak:
                        print(
                            f"Skipping empty text part {i} in line {line_index} ('{actual_text_content[:50]}...') after processing"
                        )
                        continue

                    voice_to_use_for_this_part = ""
                    if type.lower() == "multi_voice":
                        # For multi-voice, the entire line (all its parts) uses the determined speaker's voice.
                        voice_to_use_for_this_part = voice_for_this_line_multi_voice
                        if (
                            not voice_to_use_for_this_part
                        ):  # Fallback if voice couldn't be determined
                            print(
                                f"Warning: No specific voice for multi-voice part, falling back to narrator for line {line_index}"
                            )
                            voice_to_use_for_this_part = narrator_voice
                    else:  # single_voice
                        # For single-voice, distinguish between narration and dialogue voices.
                        voice_to_use_for_this_part = (
                            narrator_voice
                            if part["type"] == "narration"
                            else dialogue_voice
                        )

                    try:

                        current_part_audio_buffer = await generate_tts_with_retry(
                            MODEL,
                            voice_to_use_for_this_part,  # Use the correctly determined voice
                            text_to_speak,
                            API_OUTPUT_FORMAT,
                            speed=0.85,
                            max_retries=5,
                            task_id=task_id,
                        )

                        part_file_path = os.path.join(
                            temp_line_audio_dir,
                            f"line_{line_index:06d}_part_{i}.{API_OUTPUT_FORMAT}",
                        )

                        with open(part_file_path, "wb") as part_file:
                            part_file.write(current_part_audio_buffer)
                        audio_parts.append(part_file_path)
                        print(
                            f"[DEBUG] Created part file: {part_file_path} ({len(current_part_audio_buffer)} bytes)"
                        )
                    except asyncio.CancelledError:
                        # Clean up any created files and remove final line file before re-raising
                        for part_file in audio_parts:
                            if os.path.exists(part_file):
                                os.remove(part_file)
                        if os.path.exists(line_audio_path):
                            os.remove(line_audio_path)
                        raise
                    except Exception as e:
                        print(
                            f"CRITICAL ERROR: TTS failed after all retries for part type '{part['type']}', voice '{voice_to_use_for_this_part}', text: '{text_to_speak[:50]}...': {e}"
                        )
                        # Clean up any created files and remove final line file before re-raising
                        for part_file in audio_parts:
                            if os.path.exists(part_file):
                                os.remove(part_file)
                        if os.path.exists(line_audio_path):
                            os.remove(line_audio_path)
                        raise e

            except Exception as e:
                # Clean up any created files and remove final line file before re-raising
                for part_file in audio_parts:
                    if os.path.exists(part_file):
                        os.remove(part_file)
                if os.path.exists(line_audio_path):
                    os.remove(line_audio_path)
                print(f"ERROR processing line {line_index}: {e}")
                raise e

            if audio_parts:
                concatenate_audio_files(audio_parts, line_audio_path, API_OUTPUT_FORMAT)
                # Clean up individual part files after successful concatenation
                for part_file in audio_parts:
                    if os.path.exists(part_file):
                        os.remove(part_file)
                        print(f"[DEBUG] Cleaned up part file: {part_file}")
            else:
                print(f"WARNING: Line {line_index} resulted in no valid audio parts.")
                # Create an empty file to mark this line as processed
                with open(line_audio_path, "wb") as f:
                    f.write(b"")

            return await update_progress_and_task_status(
                line_index, actual_text_content
            )

    # Create tasks and store them with their index for result collection
    tasks = []
    task_to_index = {}
    for i, line_content in enumerate(lines_to_process):  # Iterate over lines_to_process
        if i < resume_index:
            # Already processed, skip
            continue

        task = asyncio.create_task(
            process_single_line(i, line_content, type)
        )  # Pass line_content
        tasks.append(task)
        task_to_index[task] = i

    # Initialize results_all list
    results_all = [None] * total_lines  # Use total_lines for sizing results_all

    # Create a cancellation monitor task
    async def cancellation_monitor():
        while tasks:
            await asyncio.sleep(0.5)  # Check every 500ms
            if task_id and is_task_cancelled(task_id):
                print(
                    f"[DEBUG] Cancellation monitor detected task {task_id} is cancelled"
                )
                # Cancel all remaining tasks
                for task in tasks:
                    if not task.done():
                        task.cancel()
                        print(f"[DEBUG] Cancellation monitor cancelled a task")
                # clean up the temp directory
                temp_book_dir = f"{TEMP_DIR}/{book_title}"
                if os.path.exists(temp_book_dir):
                    try:
                        shutil.rmtree(temp_book_dir)
                        print(f"[DEBUG] Cleaned up temp directory: {temp_book_dir}")
                    except Exception as e:
                        print(f"[DEBUG] Error cleaning up temp directory: {e}")
                break

    # Start the cancellation monitor
    monitor_task = asyncio.create_task(cancellation_monitor())

    # Process tasks with progress updates and retry logic
    last_reported = -1

    while tasks:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        # Store results as tasks complete
        for completed_task in done:
            idx = task_to_index[completed_task]
            results_all[idx] = completed_task.result()

        tasks = list(pending)

        # Only yield if the counter has changed
        if progress_counter > last_reported:
            last_reported = progress_counter
            percent = (progress_counter / total_lines) * 100

            # Check if task has been cancelled
            if task_id and is_task_cancelled(task_id):
                print(
                    f"[DEBUG] Task {task_id} was cancelled, cancelling all pending tasks"
                )
                # Cancel all pending tasks immediately
                for task in tasks:
                    if not task.done():
                        task.cancel()
                        print(f"[DEBUG] Cancelled pending task")
                raise asyncio.CancelledError("Task was cancelled by user")

            # update the task status
            update_task_status(
                task_id,
                "generating",
                f"Generating audiobook. Progress: {percent:.1f}%",
            )
            yield f"Generating audiobook. Progress: {percent:.1f}%"

    # All tasks have completed at this point and results_all is populated
    results = [r for r in results_all if r is not None]  # Filter out empty lines

    # Clean up the monitor task
    if not monitor_task.done():
        monitor_task.cancel()

    progress_bar.update(total_lines)
    progress_bar.close()

    results = [r for r in results_all if r is not None]

    yield f"Completed generating audio for {len(results)}/{total_lines} lines"

    # Validate all audio files exist before proceeding to concatenation
    print("Validating audio files before concatenation...")
    missing_files = []
    for result in results:
        line_idx = result["index"]
        final_line_path = os.path.join(
            temp_line_audio_dir,
            f"line_{line_idx:06d}.{API_OUTPUT_FORMAT}",
        )
        if not os.path.exists(final_line_path) or os.path.getsize(final_line_path) == 0:
            missing_files.append(line_idx)

    if missing_files:
        print(
            f"ERROR: {len(missing_files)} audio files are missing or empty: {missing_files[:10]}..."
        )
        raise Exception(
            f"Cannot proceed with concatenation - {len(missing_files)} audio files are missing"
        )

    print(f"✅ All {len(results)} audio files validated successfully")

    # Second pass: Organize by chapters
    chapter_organization_bar = tqdm(
        total=len(results), unit="result", desc="Organizing Chapters"
    )

    for result in sorted(results, key=lambda x: x["index"]):
        # Check if this is a chapter heading
        if result["is_chapter_heading"]:
            chapter_index += 1

            if MODEL == "orpheus":
                current_chapter_audio = f"{sanitize_filename(result['line'])}.m4a"
            else:
                current_chapter_audio = (
                    f"{sanitize_filename(result['line'])}.{output_format}"
                )

        if current_chapter_audio not in chapter_files:
            chapter_files.append(current_chapter_audio)
            chapter_line_map[current_chapter_audio] = []

        # Add this line index to the chapter
        chapter_line_map[current_chapter_audio].append(result["index"])
        chapter_organization_bar.update(1)

    chapter_organization_bar.close()
    yield "Organizing audio by chapters complete"

    concatenate_chapters(
        chapter_files, book_title, chapter_line_map, temp_line_audio_dir
    )
  

    # Optimized parallel post-processing
    yield "Starting parallel post-processing..."
    m4a_chapter_files = await parallel_post_processing(
        chapter_files, book_title, output_format
    )
    yield f"Completed parallel post-processing of {len(m4a_chapter_files)} chapters"

    # Clean up temp line audio files
    # shutil.rmtree(temp_line_audio_dir)
    # yield "Cleaned up temporary files"

    # create audiobook directory if it does not exist
    os.makedirs(f"generated_audiobooks", exist_ok=True)

    if generate_m4b_audiobook_file:
        # Merge all chapter files into a final m4b audiobook
        yield "Creating M4B audiobook file..."
        merge_chapters_to_m4b(book_path, m4a_chapter_files, book_title)
        # clean the temp directory
        shutil.rmtree(f"{TEMP_DIR}/{book_title}")
        yield "M4B audiobook created successfully"
    else:
        # Merge all chapter files into a standard M4A audiobook
        yield "Creating final audiobook..."
        merge_chapters_to_standard_audio_file(m4a_chapter_files, book_title)
        safe_book_title = sanitize_book_title_for_filename(book_title)
        # always convert to m4a first
        convert_audio_file_formats(
            "m4a", output_format, "generated_audiobooks", safe_book_title
        )
        yield f"Audiobook in {output_format} format created successfully"


async def process_audiobook_generation(
    voice_option,
    narrator_gender,
    output_format,
    book_path,
    book_title="audiobook",
    task_id=None,
):
    # Select narrator voice string based on narrator_gender and MODEL
    narrator_voice = select_tts_voice(MODEL, narrator_gender)

    is_tts_api_up, message = await check_tts_api(
        async_openai_client, MODEL, narrator_voice
    )

    if not is_tts_api_up:
        raise Exception(message)

    generate_m4b_audiobook_file = False
    # Determine the actual format to use for intermediate files
    actual_output_format = output_format

    if output_format == "M4B (Chapters & Cover)":
        generate_m4b_audiobook_file = True
        actual_output_format = "m4a"  # Use m4a for intermediate files when creating M4B

    if voice_option == "Single Voice":
        yield "\n🎧 Generating audiobook with a **single voice**..."
        await asyncio.sleep(1)
        async for line in generate_audio_files(
            actual_output_format.lower(),
            narrator_gender,
            generate_m4b_audiobook_file,
            book_path,
            book_title,
            "single_voice",
            task_id,
        ):
            yield line
    elif voice_option == "Multi-Voice":
        yield "\n🎭 Generating audiobook with **multiple voices**..."
        await asyncio.sleep(1)
        async for line in generate_audio_files(
            actual_output_format.lower(),
            narrator_gender,
            generate_m4b_audiobook_file,
            book_path,
            book_title,
            "multi_voice",
            task_id,
        ):
            yield line

    yield f"\n🎧 Audiobook is generated ! You can now download it in the Download section below. Click on the blue download link next to the file name."


async def main(
    book_title="audiobook",
    book_path="./sample_book_and_audio/The Adventure of the Lost Treasure - Prakhar Sharma.epub",
    output_format="aac",
    generate_m4b_audiobook_file=False,
    voice_option="single",
    narrator_gender="male",
):
    os.makedirs(f"{TEMP_DIR}/{book_title}/generated_audiobooks", exist_ok=True)

    # Check if we're running with command line arguments (non-interactive mode)
    running_with_args = (
        len(sys.argv) > 1 or voice_option != "single" or generate_m4b_audiobook_file
    )

    if not running_with_args:
        # Prompt user for voice selection
        print("\n🎙️ **Audiobook Voice Selection**")
        voice_option = input(
            "🔹 Enter **1** for **Single Voice** or **2** for **Multiple Voices**: "
        ).strip()

        # Prompt user for audiobook type selection
        print("\n🎙️ **Audiobook Type Selection**")
        print(
            "🔹 Do you want the audiobook in M4B format (the standard format for audiobooks) with chapter timestamps and embedded book cover ? (Needs calibre and ffmpeg installed)"
        )
        print(
            "🔹 OR do you want a standard audio file in either of ['aac', 'm4a', 'mp3', 'wav', 'opus', 'flac', 'pcm'] formats without any of the above features ?"
        )
        audiobook_type_option = input(
            "🔹 Enter **1** for **M4B audiobook format** or **2** for **Standard Audio File**: "
        ).strip()
    else:
        # Running with arguments, set defaults based on parameters
        if voice_option == "single" or voice_option == "1":
            voice_option = "1"
        else:
            voice_option = "2"
        audiobook_type_option = "1" if generate_m4b_audiobook_file else "2"

    if audiobook_type_option == "1":
        is_calibre_installed = check_if_calibre_is_installed()

        if not is_calibre_installed:
            print(
                "⚠️ Calibre is not installed. Please install it first and make sure **calibre** and **ebook-meta** commands are available in your PATH. Defaulting to standard audio file format."
            )

        is_ffmpeg_installed = check_if_ffmpeg_is_installed()

        if not is_ffmpeg_installed:
            print(
                "⚠️ FFMpeg is not installed. Please install it first and make sure **ffmpeg** and **ffprobe** commands are available in your PATH."
            )
            return

        # Check if a path is provided via command-line arguments
        if len(sys.argv) > 1:
            book_path = sys.argv[1]
            print(f"📂 Using book file from command-line argument: **{book_path}**")
        elif not running_with_args:
            # Ask user for book file path if not provided and in interactive mode
            if (
                book_path is None
                or book_path
                == "./sample_book_and_audio/The Adventure of the Lost Treasure - Prakhar Sharma.epub"
            ):
                input_path = input(
                    "\n📖 Enter the **path to the book file**, needed for metadata and cover extraction. (Press Enter to use default): "
                ).strip()
                if input_path:
                    book_path = input_path

        print(f"📂 Using book file: **{book_path}**")

        print("✅ Book path set. Proceeding...\n")

        generate_m4b_audiobook_file = True
    else:
        # Prompt user for audio format selection
        print("\n🎙️ **Audiobook Output Format Selection**")
        output_format = input(
            "🔹 Choose between ['aac', 'm4a', 'mp3', 'wav', 'opus', 'flac', 'pcm']. "
        ).strip()

        if output_format not in ["aac", "m4a", "mp3", "wav", "opus", "flac", "pcm"]:
            print("\n⚠️ Invalid output format! Please choose from the give options")
            return

    if not running_with_args:
        # Prompt user for narrator's gender selection
        print("\n🎙️ **Audiobook Narrator Voice Selection**")
        narrator_gender = input(
            "🔹 Enter **male** if you want the book to be read in a male voice or **female** if you want the book to be read in a female voice: "
        ).strip()

    if narrator_gender not in ["male", "female"]:
        print("\n⚠️ Using default narrator gender: male")
        narrator_gender = "male"

    start_time = time.time()

    if voice_option == "1":
        print("\n🎧 Generating audiobook with a **single voice**...")
        async for line in generate_audio_files(
            output_format,
            narrator_gender,
            generate_m4b_audiobook_file,
            book_path,
            book_title,
            "single_voice",
            f"non_interactive_{voice_option}_{narrator_gender}_{output_format}",
        ):
            print(line)
    elif voice_option == "2":
        print("\n🎭 Generating audiobook with **multiple voices**...")
        async for line in generate_audio_files(
            output_format,
            narrator_gender,
            generate_m4b_audiobook_file,
            book_path,
            book_title,
            "multi_voice",
            f"non_interactive_{voice_option}_{narrator_gender}_{output_format}",
        ):
            print(line)
    else:
        print("\n⚠️ Invalid option! Please restart and enter either **1** or **2**.")
        return

    print(
        f"\n🎧 Audiobook is generated ! The audiobook is saved as **audiobook.{"m4b" if generate_m4b_audiobook_file else output_format}** in the **generated_audiobooks** directory in the current folder."
    )

    end_time = time.time()

    execution_time = end_time - start_time
    print(
        f"\n⏱️ **Execution Time:** {execution_time:.6f} seconds\n✅ Audiobook generation complete!"
    )


if __name__ == "__main__":
    asyncio.run(main())
