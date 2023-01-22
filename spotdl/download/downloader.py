"""
Downloader module, this is where all the downloading pre/post processing happens etc.
"""

import asyncio
import concurrent.futures
import datetime
import json
import logging
import shutil
import sys
import traceback
from argparse import Namespace
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Type, Union

from yt_dlp.postprocessor.modify_chapters import ModifyChaptersPP
from yt_dlp.postprocessor.sponsorblock import SponsorBlockPP

from spotdl.download.progress_handler import ProgressHandler
from spotdl.providers.audio import AudioProvider, YouTube, YouTubeMusic
from spotdl.providers.lyrics import AzLyrics, Genius, LyricsProvider, MusixMatch, Synced
from spotdl.types.options import DownloaderOptionalOptions, DownloaderOptions
from spotdl.types.song import Song
from spotdl.utils.archive import Archive
from spotdl.utils.config import (
    DOWNLOADER_OPTIONS,
    create_settings_type,
    get_errors_path,
    get_temp_path,
)
from spotdl.utils.ffmpeg import FFmpegError, convert, get_ffmpeg_path
from spotdl.utils.formatter import create_file_name, restrict_filename
from spotdl.utils.m3u import gen_m3u_files
from spotdl.utils.metadata import MetadataError, embed_metadata
from spotdl.utils.search import gather_known_songs, reinit_song, songs_from_albums

__all__ = [
    "AUDIO_PROVIDERS",
    "LYRICS_PROVIDERS",
    "Downloader",
    "DownloaderError",
    "SPONSOR_BLOCK_CATEGORIES",
]

AUDIO_PROVIDERS: Dict[str, Type[AudioProvider]] = {
    "youtube": YouTube,
    "youtube-music": YouTubeMusic,
}

LYRICS_PROVIDERS: Dict[str, Type[LyricsProvider]] = {
    "genius": Genius,
    "musixmatch": MusixMatch,
    "azlyrics": AzLyrics,
    "synced": Synced,
}

SPONSOR_BLOCK_CATEGORIES = {
    "sponsor": "Sponsor",
    "intro": "Intermission/Intro Animation",
    "outro": "Endcards/Credits",
    "selfpromo": "Unpaid/Self Promotion",
    "preview": "Preview/Recap",
    "filler": "Filler Tangent",
    "interaction": "Interaction Reminder",
    "music_offtopic": "Non-Music Section",
}

logger = logging.getLogger(__name__)


class DownloaderError(Exception):
    """
    Base class for all exceptions related to downloaders.
    """


class Downloader:
    """
    Downloader class, this is where all the downloading pre/post processing happens etc.
    It handles the downloading/moving songs, multithreading, metadata embedding etc.
    """

    def __init__(
        self,
        settings: Optional[Union[DownloaderOptionalOptions, DownloaderOptions]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        """
        Initialize the Downloader class.

        ### Arguments
        - settings: The settings to use.
        - loop: The event loop to use.

        ### Notes
        - `search-query` uses the same format as `output`.
        - if `audio_provider` or `lyrics_provider` is a list, then if no match is found,
            the next provider in the list will be used.
        """

        if settings is None:
            settings = {}

        # Create settings dictionary, fill in missing values with defaults
        # from spotdl.types.options.DOWNLOADER_OPTIONS
        self.settings: DownloaderOptions = DownloaderOptions(
            **create_settings_type(
                Namespace(config=False), dict(settings), DOWNLOADER_OPTIONS
            )  # type: ignore
        )

        logger.debug("Downloader settings: %s", self.settings)

        # If no audio providers specified, raise an error
        if len(self.settings["audio_providers"]) == 0:
            raise DownloaderError(
                "No audio providers specified. Please specify at least one."
            )

        # If ffmpeg is the default value and it's not installed
        # try to use the spotdl's ffmpeg
        self.ffmpeg = self.settings["ffmpeg"]
        if self.ffmpeg == "ffmpeg" and shutil.which("ffmpeg") is None:
            ffmpeg_exec = get_ffmpeg_path()
            if ffmpeg_exec is None:
                raise DownloaderError("ffmpeg is not installed")

            self.ffmpeg = str(ffmpeg_exec.absolute())

        logger.debug("FFmpeg path: %s", self.ffmpeg)

        self.loop = loop or (
            asyncio.new_event_loop()
            if sys.platform != "win32"
            else asyncio.ProactorEventLoop()  # type: ignore
        )

        if loop is None:
            asyncio.set_event_loop(self.loop)

        # semaphore is required to limit concurrent asyncio executions
        self.semaphore = asyncio.Semaphore(self.settings["threads"])

        # thread pool executor is used to run blocking (CPU-bound) code from a thread
        self.thread_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.settings["threads"]
        )

        self.progress_handler = ProgressHandler(self.settings["simple_tui"])

        # Gather already present songs
        self.known_songs: Dict[str, List[Path]] = {}
        if self.settings["scan_for_songs"]:
            logger.info("Scanning for known songs, this might take a while...")

            self.known_songs = gather_known_songs(
                self.settings["output"], self.settings["format"]
            )

        logger.debug("Found %s known songs", len(self.known_songs))

        # Initialize lyrics providers
        self.lyrics_providers: List[LyricsProvider] = []
        for lyrics_provider in self.settings["lyrics_providers"]:
            lyrics_class = LYRICS_PROVIDERS.get(lyrics_provider)
            if lyrics_class is None:
                raise DownloaderError(f"Invalid lyrics provider: {lyrics_provider}")

            self.lyrics_providers.append(lyrics_class())

        # Initialize audio providers
        self.audio_providers: List[AudioProvider] = []
        for audio_provider in self.settings["audio_providers"]:
            audio_class = AUDIO_PROVIDERS.get(audio_provider)
            if audio_class is None:
                raise DownloaderError(f"Invalid audio provider: {audio_provider}")

            self.audio_providers.append(
                audio_class(
                    output_format=self.settings["format"],
                    cookie_file=self.settings["cookie_file"],
                    search_query=self.settings["search_query"],
                    filter_results=self.settings["filter_results"],
                )
            )

        # Initialize list of errors
        self.errors: List[str] = []

        # Initialize archive
        self.url_archive = Archive()
        if self.settings["archive"]:
            self.url_archive.load(self.settings["archive"])
        logger.debug("Archive: %d urls", len(self.url_archive))

        logger.debug("Downloader initialized")

    def download_song(self, song: Song) -> Tuple[Song, Optional[Path]]:
        """
        Download a single song.

        ### Arguments
        - song: The song to download.

        ### Returns
        - tuple with the song and the path to the downloaded file if successful.
        """

        self.progress_handler.set_song_count(1)

        results = self.download_multiple_songs([song])

        return results[0]

    def download_multiple_songs(
        self, songs: List[Song]
    ) -> List[Tuple[Song, Optional[Path]]]:
        """
        Download multiple songs to the temp directory.

        ### Arguments
        - songs: The songs to download.

        ### Returns
        - list of tuples with the song and the path to the downloaded file if successful.
        """

        if self.settings["fetch_albums"]:
            album_ids = []
            for song in songs:
                songmeta = Song.from_url(song.url)
                if songmeta.album_id is not None:
                    album_ids.append(songmeta.album_id)
                    logger.debug(
                        "Found song %d's album ID (%s)",
                        songmeta.name,
                        songmeta.album_id,
                    )
            albums = set(str(id) for id in album_ids)
            logger.info(
                "Fetching %d album%s", len(albums), "s" if len(albums) > 1 else ""
            )

            songs.extend(songs_from_albums(list(albums)))

            # Remove duplicates
            return_obj = {}
            for song in songs:
                return_obj[song.url] = song

            songs = list(return_obj.values())

        logger.debug("Downloading %d songs", len(songs))

        if self.settings["archive"]:
            songs = [song for song in songs if song.url not in self.url_archive]
            logger.debug("Filtered %d songs with archive", len(songs))

        self.progress_handler.set_song_count(len(songs))

        # Create tasks list
        tasks = [self.pool_download(song) for song in songs]

        # Call all task asynchronously, and wait until all are finished
        results = list(self.loop.run_until_complete(asyncio.gather(*tasks)))

        # Print errors
        if self.settings["print_errors"]:
            for error in self.errors:
                logger.error(error)

        # Save archive
        if self.settings["archive"]:
            for result in results:
                if result[1]:
                    self.url_archive.add(result[0].url)

            self.url_archive.save(self.settings["archive"])
            logger.info(
                "Saved archive with %d urls to %s",
                len(self.url_archive),
                self.settings["archive"],
            )

        # Create m3u playlist
        if self.settings["m3u"]:
            song_list = [song for song, _ in results]
            gen_m3u_files(
                song_list,
                self.settings["m3u"],
                self.settings["output"],
                self.settings["format"],
                False,
            )

        # Save results to a file
        if self.settings["save_file"]:
            with open(self.settings["save_file"], "w", encoding="utf-8") as save_file:
                json.dump([song.json for song, _ in results], save_file, indent=4)

            logger.info("Saved results to %s", self.settings["save_file"])

        return results

    async def pool_download(self, song: Song) -> Tuple[Song, Optional[Path]]:
        """
        Run asynchronous task in a pool to make sure that all processes.

        ### Arguments
        - song: The song to download.

        ### Returns
        - tuple with the song and the path to the downloaded file if successful.

        ### Notes
        - This method calls `self.search_and_download` in a new thread.
        """

        # tasks that cannot acquire semaphore will wait here until it's free
        # only certain amount of tasks can acquire the semaphore at the same time
        async with self.semaphore:
            # The following function calls blocking code, which would block whole event loop.
            # Therefore, it has to be called in a separate thread via ThreadPoolExecutor. This
            # is not a problem, since GIL is released for the I/O operations, so it shouldn't
            # hurt performance.
            return await self.loop.run_in_executor(
                self.thread_executor, self.search_and_download, song
            )

    def search(self, song: Song) -> str:
        """
        Search for a song using all available providers.

        ### Arguments
        - song: The song to search for.

        ### Returns
        - tuple with download url and audio provider if successful.
        """

        for audio_provider in self.audio_providers:
            url = audio_provider.search(song)
            if url:
                return url

            logger.debug("%s failed to find %s", audio_provider.name, song.display_name)

        raise LookupError(f"No results found for song: {song.display_name}")

    def search_lyrics(self, song: Song) -> Optional[str]:
        """
        Search for lyrics using all available providers.

        ### Arguments
        - song: The song to search for.

        ### Returns
        - lyrics if successful else None.
        """

        for lyrics_provider in self.lyrics_providers:
            lyrics = lyrics_provider.get_lyrics(song.name, song.artists)
            if lyrics:
                logger.debug(
                    "Found lyrics for %s on %s", song.display_name, lyrics_provider.name
                )

                return lyrics

            logger.debug(
                "%s failed to find lyrics for %s",
                lyrics_provider.name,
                song.display_name,
            )

        return None

    def search_and_download(self, song: Song) -> Tuple[Song, Optional[Path]]:
        """
        Search for the song and download it.

        ### Arguments
        - song: The song to download.

        ### Returns
        - tuple with the song and the path to the downloaded file if successful.

        ### Notes
        - This function is synchronous.
        """

        # Check if we have all the metadata
        # and that the song object is not a placeholder
        # If it's None extract the current metadata
        # And reinitialize the song object
        if song.name is None and song.url:
            song = reinit_song(song, self.settings["playlist_numbering"])

        # Initalize the progress tracker
        display_progress_tracker = self.progress_handler.get_new_tracker(song)

        # Create the output file path
        output_file = create_file_name(
            song, self.settings["output"], self.settings["format"]
        )
        temp_folder = get_temp_path()

        # Restrict the filename if needed
        if self.settings["restrict"] is True:
            output_file = restrict_filename(output_file)

        # Check if there is an already existing song file, with the same spotify URL in its
        # metadata, but saved under a different name. If so, save its path.
        dup_song_paths: List[Path] = self.known_songs.get(song.url, [])

        # Remove files from the list that have the same path as the output file
        dup_song_paths = [
            dup_song_path
            for dup_song_path in dup_song_paths
            if (dup_song_path.absolute() != output_file.absolute())
            and dup_song_path.exists()
        ]

        if dup_song_paths:
            logger.debug(
                "Found duplicate songs for %s at %s", song.display_name, dup_song_paths
            )

        # If the file already exists and we don't want to overwrite it,
        # we can skip the download
        if (output_file.exists() or dup_song_paths) and self.settings[
            "overwrite"
        ] == "skip":
            logger.info(
                "Skipping %s (file already exists) %s",
                song.display_name,
                "(duplicate)" if dup_song_paths else "",
            )

            display_progress_tracker.notify_download_skip()
            return song, None

        # Don't skip if the file exists and overwrite is set to force
        if (output_file.exists() or dup_song_paths) and self.settings[
            "overwrite"
        ] == "force":
            logger.info(
                "Overwriting %s %s",
                song.display_name,
                " (duplicate)" if dup_song_paths else "",
            )

            # If the duplicate song path is not None, we can delete the old file
            for dup_song_path in dup_song_paths:
                try:
                    logger.info("Removing duplicate file: %s", dup_song_path)

                    dup_song_path.unlink()
                except (PermissionError, OSError) as exc:
                    logger.debug(
                        "Could not remove duplicate file: %s, error: %s",
                        dup_song_path,
                        exc,
                    )

        # Find song lyrics and add them to the song object
        lyrics = self.search_lyrics(song)
        if lyrics is None:
            logger.debug(
                "No lyrics found for %s, lyrics providers: %s",
                song.display_name,
                ", ".join([lprovider.name for lprovider in self.lyrics_providers]),
            )
        else:
            song.lyrics = lyrics

        # If the file already exists and we want to overwrite the metadata,
        # we can skip the download
        if (output_file.exists() or dup_song_paths) and self.settings[
            "overwrite"
        ] == "metadata":
            most_recent_duplicate: Optional[Path] = None
            if dup_song_paths:
                # Get the most recent duplicate song path and remove the rest
                most_recent_duplicate = max(
                    dup_song_paths,
                    key=lambda dup_song_path: dup_song_path.stat().st_mtime,
                )

                # Remove the rest of the duplicate song paths
                for old_song_path in dup_song_paths:
                    if most_recent_duplicate == old_song_path:
                        continue

                    try:
                        logger.info("Removing duplicate file: %s", old_song_path)
                        old_song_path.unlink()
                    except (PermissionError, OSError) as exc:
                        logger.debug(
                            "Could not remove duplicate file: %s, error: %s",
                            old_song_path,
                            exc,
                        )

                # Move the old file to the new location
                if most_recent_duplicate:
                    most_recent_duplicate.replace(
                        output_file.with_suffix(f".{self.settings['format']}")
                    )

            # Update the metadata
            embed_metadata(output_file=output_file, song=song)

            logger.info(
                f"Updated metadata for {song.display_name}"
                f", moved to new location: {output_file}"
                if most_recent_duplicate
                else ""
            )

            display_progress_tracker.notify_complete()

            return song, output_file

        # Create the output directory if it doesn't exist
        output_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            if song.download_url is None:
                download_url = self.search(song)
            else:
                download_url = song.download_url

            # Initialize audio downloader
            audio_downloader = AudioProvider(
                output_format=self.settings["format"],
                cookie_file=self.settings["cookie_file"],
                search_query=self.settings["search_query"],
                filter_results=self.settings["filter_results"],
            )

            logger.debug("Downloading %s using %s", song.display_name, download_url)

            # Add progress hook to the audio provider
            audio_downloader.audio_handler.add_progress_hook(
                display_progress_tracker.yt_dlp_progress_hook
            )

            # Download the song using yt-dlp
            download_info = audio_downloader.get_download_metadata(
                download_url, download=True
            )

            temp_file = Path(
                temp_folder / f"{download_info['id']}.{download_info['ext']}"
            )

            if download_info is None:
                logger.debug(
                    "No download info found for %s, url: %s",
                    song.display_name,
                    download_url,
                )

                raise DownloaderError(
                    f"yt-dlp failed to get metadata for: {song.name} - {song.artist}"
                )

            display_progress_tracker.notify_download_complete()

            # Ignore the bitrate if the bitrate is set to auto for m4a/opus
            # or if bitrate is set to disabled
            if self.settings["bitrate"] == "disable":
                bitrate = None
            elif self.settings["bitrate"] == "auto" or self.settings["bitrate"] is None:
                bitrate = f"{int(download_info['abr'])}k"
            else:
                bitrate = str(self.settings["bitrate"])

            success, result = convert(
                input_file=temp_file,
                output_file=output_file,
                ffmpeg=self.ffmpeg,
                output_format=self.settings["format"],
                bitrate=bitrate,
                ffmpeg_args=self.settings["ffmpeg_args"],
                progress_handler=display_progress_tracker.ffmpeg_progress_hook,
            )

            # Remove the temp file
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except (PermissionError, OSError) as exc:
                    logger.debug(
                        "Could not remove temp file: %s, error: %s", temp_file, exc
                    )

                    raise DownloaderError(
                        f"Could not remove temp file: {temp_file}, possible duplicate song"
                    ) from exc

            if not success and result:
                # If the conversion failed and there is an error message
                # create a file with the error message
                # and save it in the errors directory
                # raise an exception with file path
                file_name = (
                    get_errors_path()
                    / f"ffmpeg_error_{datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}.txt"
                )

                error_message = ""
                for key, value in result.items():
                    error_message += f"### {key}:\n{str(value).strip()}\n\n"

                with open(file_name, "w", encoding="utf-8") as error_path:
                    error_path.write(error_message)

                # Remove the file that failed to convert
                if output_file.exists():
                    output_file.unlink()

                raise FFmpegError(
                    f"Failed to convert {song.display_name}, "
                    f"you can find error here: {str(file_name.absolute())}"
                )

            download_info["filepath"] = str(output_file)

            # Set the song's download url
            if song.download_url is None:
                song.download_url = download_url

            display_progress_tracker.notify_conversion_complete()

            # SponsorBlock post processor
            if self.settings["sponsor_block"]:
                # Initialize the sponsorblock post processor
                post_processor = SponsorBlockPP(
                    audio_downloader.audio_handler, SPONSOR_BLOCK_CATEGORIES
                )

                # Run the post processor to get the sponsor segments
                _, download_info = post_processor.run(download_info)
                chapters = download_info["sponsorblock_chapters"]

                # If there are sponsor segments, remove them
                if len(chapters) > 0:
                    logger.info(
                        "Removing %s sponsor segments for %s",
                        len(chapters),
                        song.display_name,
                    )

                    # Initialize the modify chapters post processor
                    modify_chapters = ModifyChaptersPP(
                        audio_downloader.audio_handler,
                        remove_sponsor_segments=SPONSOR_BLOCK_CATEGORIES,
                    )

                    # Run the post processor to remove the sponsor segments
                    # this returns a list of files to delete
                    files_to_delete, download_info = modify_chapters.run(download_info)

                    # Delete the files that were created by the post processor
                    for file_to_delete in files_to_delete:
                        Path(file_to_delete).unlink()

            try:
                embed_metadata(output_file, song, self.settings["id3_separator"])
            except Exception as exception:
                raise MetadataError(
                    "Failed to embed metadata to the song"
                ) from exception

            display_progress_tracker.notify_complete()

            # Add the song to the known songs
            self.known_songs.get(song.url, []).append(output_file)

            logger.info('Downloaded "%s": %s', song.display_name, song.download_url)

            return song, output_file
        except (Exception, UnicodeEncodeError) as exception:
            if isinstance(exception, UnicodeEncodeError):
                exception_cause = exception
                exception = DownloaderError(
                    "You may need to add PYTHONIOENCODING=utf-8 to your environment"
                )

                exception.__cause__ = exception_cause

            display_progress_tracker.notify_error(
                traceback.format_exc(), exception, True
            )
            self.errors.append(
                f"{song.url} - {exception.__class__.__name__}: {exception}"
            )
            return song, None
