"""Writing correct tags (Picard conventions) so Lidarr can match releases.

Lidarr's import matching reads embedded MusicBrainz IDs first — writing
MUSICBRAINZ_ALBUMID / MUSICBRAINZ_TRACKID etc. makes its identification
close to deterministic. Existing unrelated tags (ReplayGain, ratings, …)
are preserved unless clear_existing_tags is enabled.
"""

from __future__ import annotations

from dataclasses import dataclass

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    ID3,
    TALB,
    TDRC,
    TIT2,
    TMED,
    TPE1,
    TPE2,
    TPOS,
    TPUB,
    TRCK,
    TSO2,
    TSOP,
    TXXX,
    UFID,
    ID3NoHeaderError,
)
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from printarr.config import TaggingConfig
from printarr.log import get_logger
from printarr.musicbrainz import MBRelease, MBTrack

log = get_logger(__name__)


class TaggingError(Exception):
    pass


@dataclass
class TrackTags:
    """Resolved tag values for one file."""
    title: str
    artist: str
    album: str
    album_artist: str
    track: int
    track_total: int
    disc: int
    disc_total: int
    date: str
    year: str
    artist_sort: str
    label: str
    catalog_number: str
    barcode: str
    country: str
    media: str
    status: str
    release_type: str
    mb_release_id: str
    mb_release_group_id: str
    mb_recording_id: str
    mb_track_id: str
    mb_artist_ids: list[str]
    mb_album_artist_ids: list[str]


def build_track_tags(release: MBRelease, track: MBTrack) -> TrackTags:
    tracks_on_medium = [t for t in release.tracks if t.medium == track.medium]
    media_format = ""
    if release.media_formats and 0 < track.medium <= len(release.media_formats):
        media_format = release.media_formats[track.medium - 1]
    return TrackTags(
        title=track.title,
        artist=track.artist or release.artist,
        album=release.title,
        album_artist=release.artist,
        track=track.position,
        track_total=len(tracks_on_medium),
        disc=track.medium,
        disc_total=release.disc_count,
        date=release.date,
        year=release.year,
        artist_sort=release.artist_sort,
        label=release.label,
        catalog_number=release.catalog_number,
        barcode=release.barcode,
        country=release.country,
        media=media_format,
        status=release.status,
        release_type=release.release_group_type,
        mb_release_id=release.id,
        mb_release_group_id=release.release_group_id,
        mb_recording_id=track.recording_id,
        mb_track_id=track.id,
        mb_artist_ids=release.artist_ids,
        mb_album_artist_ids=release.artist_ids,
    )


def write_tags(path, tags: TrackTags, config: TaggingConfig,
               cover: bytes | None = None, cover_mime: str = "image/jpeg") -> None:
    """Write tags to a file, dispatching on its container format."""
    suffix = str(path).rsplit(".", 1)[-1].lower()
    if suffix == "flac":
        _write_flac(path, tags, config, cover, cover_mime)
    elif suffix == "mp3":
        _write_id3(path, tags, config, cover, cover_mime)
    elif suffix in ("wav", "aiff", "aif"):
        _write_id3_chunk(path, tags, config, cover, cover_mime)
    elif suffix in ("m4a", "m4b", "mp4"):
        _write_mp4(path, tags, config, cover, cover_mime)
    elif suffix in ("ogg", "oga", "opus"):
        _write_ogg(path, tags, config, cover, cover_mime)
    elif suffix in ("wma", "asf"):
        _write_asf(path, tags, config)
    else:
        _write_generic(path, tags)


# ---------------------------------------------------------------- Vorbis-style

def _vorbis_fields(tags: TrackTags) -> dict[str, list[str]]:
    fields = {
        "TITLE": [tags.title],
        "ARTIST": [tags.artist],
        "ALBUM": [tags.album],
        "ALBUMARTIST": [tags.album_artist],
        "TRACKNUMBER": [str(tags.track)],
        "TRACKTOTAL": [str(tags.track_total)],
        "TOTALTRACKS": [str(tags.track_total)],
        "DISCNUMBER": [str(tags.disc)],
        "DISCTOTAL": [str(tags.disc_total)],
        "TOTALDISCS": [str(tags.disc_total)],
        "MUSICBRAINZ_ALBUMID": [tags.mb_release_id],
        "MUSICBRAINZ_RELEASEGROUPID": [tags.mb_release_group_id],
        "MUSICBRAINZ_TRACKID": [tags.mb_recording_id],
        "MUSICBRAINZ_RELEASETRACKID": [tags.mb_track_id],
        "MUSICBRAINZ_ARTISTID": tags.mb_artist_ids,
        "MUSICBRAINZ_ALBUMARTISTID": tags.mb_album_artist_ids,
    }
    optional = {
        "DATE": tags.date,
        "ARTISTSORT": tags.artist_sort,
        "ALBUMARTISTSORT": tags.artist_sort,
        "LABEL": tags.label,
        "CATALOGNUMBER": tags.catalog_number,
        "BARCODE": tags.barcode,
        "RELEASECOUNTRY": tags.country,
        "MEDIA": tags.media,
        "RELEASESTATUS": tags.status,
        "RELEASETYPE": tags.release_type,
    }
    for key, value in optional.items():
        if value:
            fields[key] = [value]
    return {key: value for key, value in fields.items() if value and value[0]}


def _apply_vorbis(audio, tags: TrackTags, config: TaggingConfig,
                  keep_keys: tuple[str, ...] = ()) -> None:
    if config.clear_existing_tags and audio.tags is not None:
        # Keep cover-art keys unless a replacement will overwrite them anyway
        preserved = {key: audio[key] for key in list(audio.keys())
                     if key.upper() in keep_keys}
        audio.tags.clear()
        for key, value in preserved.items():
            audio[key] = value
    fields = _vorbis_fields(tags)
    # Drop stale variants of the fields we are about to write
    for key in list(audio.keys()):
        if key.upper() in fields:
            del audio[key]
    for key, value in fields.items():
        audio[key] = value


def _write_flac(path, tags, config, cover, cover_mime) -> None:
    audio = FLAC(path)
    _apply_vorbis(audio, tags, config)
    if cover and config.write_cover_art:
        picture = Picture()
        picture.type = 3  # front cover
        picture.mime = cover_mime
        picture.data = cover
        audio.clear_pictures()
        audio.add_picture(picture)
    audio.save()


def _write_ogg(path, tags, config, cover, cover_mime) -> None:
    # The suffix does not identify the codec (.oga can be Ogg FLAC);
    # let mutagen sniff the actual stream type.
    audio = mutagen.File(path)
    if not isinstance(audio, (OggVorbis, OggOpus, OggFLAC)):
        raise TaggingError(f"unrecognized Ogg stream in {path}")
    _apply_vorbis(audio, tags, config,
                  keep_keys=("METADATA_BLOCK_PICTURE", "COVERART", "COVERARTMIME"))
    if cover and config.write_cover_art:
        import base64

        picture = Picture()
        picture.type = 3
        picture.mime = cover_mime
        picture.data = cover
        audio["METADATA_BLOCK_PICTURE"] = [
            base64.b64encode(picture.write()).decode("ascii")
        ]
    audio.save()


# ------------------------------------------------------------------------- ID3

def _write_id3(path, tags: TrackTags, config: TaggingConfig, cover, cover_mime) -> None:
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = ID3()
    _apply_id3_frames(id3, tags, config, cover, cover_mime)
    id3.save(path, v2_version=4)


def _write_id3_chunk(path, tags: TrackTags, config: TaggingConfig,
                     cover, cover_mime) -> None:
    """WAV and AIFF carry ID3 tags in a RIFF/IFF chunk."""
    audio = mutagen.File(path)
    if audio is None:
        raise TaggingError(f"unsupported format: {path}")
    if audio.tags is None:
        audio.add_tags()
    _apply_id3_frames(audio.tags, tags, config, cover, cover_mime)
    audio.save()


def _apply_id3_frames(id3, tags: TrackTags, config: TaggingConfig,
                      cover, cover_mime) -> None:
    if config.clear_existing_tags:
        existing_covers = id3.getall("APIC")
        id3.clear()
        for frame in existing_covers:
            id3.add(frame)

    id3.add(TIT2(encoding=3, text=[tags.title]))
    id3.add(TPE1(encoding=3, text=[tags.artist]))
    id3.add(TALB(encoding=3, text=[tags.album]))
    id3.add(TPE2(encoding=3, text=[tags.album_artist]))
    id3.add(TRCK(encoding=3, text=[f"{tags.track}/{tags.track_total}"]))
    id3.add(TPOS(encoding=3, text=[f"{tags.disc}/{tags.disc_total}"]))
    if tags.date:
        id3.add(TDRC(encoding=3, text=[tags.date]))
    if tags.artist_sort:
        id3.add(TSOP(encoding=3, text=[tags.artist_sort]))
        id3.add(TSO2(encoding=3, text=[tags.artist_sort]))
    if tags.label:
        id3.add(TPUB(encoding=3, text=[tags.label]))
    if tags.media:
        id3.add(TMED(encoding=3, text=[tags.media]))

    txxx: dict[str, list[str]] = {
        "MusicBrainz Album Id": [tags.mb_release_id],
        "MusicBrainz Release Group Id": [tags.mb_release_group_id],
        "MusicBrainz Release Track Id": [tags.mb_track_id],
        "MusicBrainz Artist Id": tags.mb_artist_ids,
        "MusicBrainz Album Artist Id": tags.mb_album_artist_ids,
        "MusicBrainz Album Status": [tags.status],
        "MusicBrainz Album Type": [tags.release_type],
        "MusicBrainz Album Release Country": [tags.country],
        "CATALOGNUMBER": [tags.catalog_number],
        "BARCODE": [tags.barcode],
    }
    for description, values in txxx.items():
        id3.delall(f"TXXX:{description}")
        values = [v for v in values if v]
        if values:
            id3.add(TXXX(encoding=3, desc=description, text=values))

    id3.delall("UFID:http://musicbrainz.org")
    if tags.mb_recording_id:
        id3.add(UFID(owner="http://musicbrainz.org",
                     data=tags.mb_recording_id.encode("ascii")))

    if cover and config.write_cover_art:
        id3.delall("APIC")
        id3.add(APIC(encoding=3, mime=cover_mime, type=3, desc="", data=cover))


# ------------------------------------------------------------------------- MP4

_MP4_FREEFORM_PREFIX = "----:com.apple.iTunes:"


def _write_mp4(path, tags: TrackTags, config: TaggingConfig, cover, cover_mime) -> None:
    audio = MP4(path)
    if config.clear_existing_tags and audio.tags is not None:
        existing_cover = audio.tags.get("covr")
        audio.tags.clear()
        if existing_cover:
            audio["covr"] = existing_cover

    audio["\xa9nam"] = [tags.title]
    audio["\xa9ART"] = [tags.artist]
    audio["\xa9alb"] = [tags.album]
    audio["aART"] = [tags.album_artist]
    audio["trkn"] = [(tags.track, tags.track_total)]
    audio["disk"] = [(tags.disc, tags.disc_total)]
    if tags.date:
        audio["\xa9day"] = [tags.date]
    if tags.artist_sort:
        audio["soar"] = [tags.artist_sort]
        audio["soaa"] = [tags.artist_sort]

    freeform: dict[str, list[str] | str] = {
        "MusicBrainz Album Id": tags.mb_release_id,
        "MusicBrainz Release Group Id": tags.mb_release_group_id,
        "MusicBrainz Track Id": tags.mb_recording_id,
        "MusicBrainz Release Track Id": tags.mb_track_id,
        "MusicBrainz Artist Id": tags.mb_artist_ids,
        "MusicBrainz Album Artist Id": tags.mb_album_artist_ids,
        "MusicBrainz Album Status": tags.status,
        "MusicBrainz Album Type": tags.release_type,
        "MusicBrainz Album Release Country": tags.country,
        "LABEL": tags.label,
        "CATALOGNUMBER": tags.catalog_number,
        "BARCODE": tags.barcode,
        "MEDIA": tags.media,
    }
    for name, value in freeform.items():
        key = _MP4_FREEFORM_PREFIX + name
        values = [v for v in (value if isinstance(value, list) else [value]) if v]
        if values:
            # One data atom per value, Picard-style
            audio[key] = [v.encode("utf-8") for v in values]
        elif key in audio:
            del audio[key]

    if cover and config.write_cover_art:
        image_format = (MP4Cover.FORMAT_PNG if cover_mime == "image/png"
                        else MP4Cover.FORMAT_JPEG)
        audio["covr"] = [MP4Cover(cover, imageformat=image_format)]

    audio.save()


# ------------------------------------------------------------------------- ASF

def _write_asf(path, tags: TrackTags, config: TaggingConfig) -> None:
    """WMA/ASF with standard WM/* attribute names (Picard conventions)."""
    from mutagen.asf import ASF

    audio = ASF(path)
    if config.clear_existing_tags and audio.tags is not None:
        audio.tags.clear()

    fields: dict[str, list[str] | str] = {
        "Title": tags.title,
        "Author": tags.artist,
        "WM/AlbumTitle": tags.album,
        "WM/AlbumArtist": tags.album_artist,
        "WM/TrackNumber": str(tags.track),
        "WM/PartOfSet": f"{tags.disc}/{tags.disc_total}",
        "WM/Year": tags.date,
        "WM/ArtistSortOrder": tags.artist_sort,
        "WM/AlbumArtistSortOrder": tags.artist_sort,
        "WM/Publisher": tags.label,
        "WM/CatalogNo": tags.catalog_number,
        "WM/Barcode": tags.barcode,
        "WM/Media": tags.media,
        "MusicBrainz/Album Id": tags.mb_release_id,
        "MusicBrainz/Release Group Id": tags.mb_release_group_id,
        "MusicBrainz/Track Id": tags.mb_recording_id,
        "MusicBrainz/Release Track Id": tags.mb_track_id,
        "MusicBrainz/Artist Id": tags.mb_artist_ids,
        "MusicBrainz/Album Artist Id": tags.mb_album_artist_ids,
        "MusicBrainz/Album Status": tags.status,
        "MusicBrainz/Album Type": tags.release_type,
        "MusicBrainz/Album Release Country": tags.country,
    }
    for key, value in fields.items():
        values = [v for v in (value if isinstance(value, list) else [value]) if v]
        if values:
            audio[key] = values
        elif key in audio:
            del audio[key]
    audio.save()


# --------------------------------------------------------------------- generic

def _write_generic(path, tags: TrackTags) -> None:
    """Best-effort tagging for less common formats via mutagen's easy interface."""
    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise TaggingError(f"unsupported format: {path}")
    basics = {
        "title": tags.title,
        "artist": tags.artist,
        "album": tags.album,
        "albumartist": tags.album_artist,
        "tracknumber": str(tags.track),
        "discnumber": str(tags.disc),
        "date": tags.date,
        "musicbrainz_albumid": tags.mb_release_id,
        "musicbrainz_trackid": tags.mb_recording_id,
        "musicbrainz_releasetrackid": tags.mb_track_id,
        "musicbrainz_artistid": "/".join(tags.mb_artist_ids),
        "musicbrainz_albumartistid": "/".join(tags.mb_album_artist_ids),
        "musicbrainz_releasegroupid": tags.mb_release_group_id,
    }
    for key, value in basics.items():
        if not value:
            continue
        try:
            audio[key] = value
        except (KeyError, ValueError, TypeError):
            log.debug("format %s does not accept tag %s", path, key)
    audio.save()
