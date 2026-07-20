"""Pure HTTP acquisition of Douyin image-text assets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from learnnest.publication import atomic_replace_bytes
from learnnest.source_models import SourceItem

_MAX_IMAGE_BYTES = 50 * 1024 * 1024
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}


class DouyinMediaError(RuntimeError):
    """A safe image-text acquisition failure without remote URL material."""


class DouyinDetailTransport(Protocol):
    """Runtime detail client; credentials remain inside its implementation."""

    def get_aweme_detail(self, aweme_id: str) -> Mapping[str, Any]: ...


class ImageTextFile(BaseModel):
    """One downloaded local image without retaining its remote URL."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    path: str = Field(min_length=1)
    bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ImageTextManifest(BaseModel):
    """Persisted local asset facts for one image-text work."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    platform: Literal["douyin"] = "douyin"
    platform_id: str = Field(min_length=1)
    source_input: str = Field(min_length=1)
    content_type: Literal["image_text"] = "image_text"
    variant: Literal["gallery", "article"] = "gallery"
    text_path: str | None = None
    title: str | None = Field(default=None, min_length=1)
    abstract: str | None = None
    files: list[ImageTextFile] = Field(default_factory=list)

    @field_validator("text_path")
    @classmethod
    def require_relative_text_path(cls, value: str | None) -> str | None:
        if value is not None:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("text_path must stay relative to the output root")
        return value

    @model_validator(mode="after")
    def validate_variant(self) -> ImageTextManifest:
        if self.variant == "gallery" and not self.files:
            raise ValueError("gallery image-text manifests require files")
        if self.variant == "article" and self.text_path is None:
            raise ValueError("article image-text manifests require text_path")
        return self


@dataclass(frozen=True)
class ImageTextDownload:
    """Result of one image-text asset acquisition."""

    manifest_path: Path
    files: tuple[Path, ...]
    text_path: Path | None = None


@dataclass(frozen=True)
class _ImageAsset:
    download_url: str
    markdown_url: str | None = None


@dataclass(frozen=True)
class _ArticlePayload:
    title: str | None
    abstract: str | None
    markdown: str
    images: tuple[_ImageAsset, ...]


class DouyinImageTextDownloader:
    """Resolve a work detail and download gallery or article assets without a browser.

    This adapter intentionally produces a local asset manifest instead of a
    synthetic video.  The existing ASR/FFmpeg pipeline remains video-only and
    can consume this contract later through a dedicated image-text stage.
    """

    def __init__(
        self,
        detail_transport: DouyinDetailTransport,
        *,
        opener: Callable[..., Any] = urlopen,
        timeout: float = 20.0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("Douyin media timeout must be positive")
        self._detail_transport = detail_transport
        self._opener = opener
        self._timeout = timeout

    def download(self, source: SourceItem, output_dir: str | Path) -> ImageTextDownload:
        if source.input_type != "url":
            raise ValueError("Douyin image-text downloader requires a URL source")
        if source.content_type != "image_text":
            raise ValueError("Douyin image-text downloader requires image_text")
        aweme_id = _aweme_id_from_url(source.input)
        detail = self._detail_transport.get_aweme_detail(aweme_id)
        root = Path(output_dir).resolve()
        article = _article_payload(detail)
        if article is not None:
            return self._download_article(source, aweme_id, root, article)

        image_assets = [
            _ImageAsset(download_url=url) for url in _gallery_image_urls(detail)
        ]
        if not image_assets:
            raise DouyinMediaError("Douyin image-text detail contains no images")
        files, paths = self._download_images(root, image_assets)
        manifest = ImageTextManifest(
            platform_id=aweme_id,
            source_input=source.input,
            files=files,
        )
        manifest_path = root / "image_text.json"
        _write_manifest(manifest_path, manifest)
        return ImageTextDownload(manifest_path=manifest_path, files=tuple(paths))

    def _download_article(
        self,
        source: SourceItem,
        aweme_id: str,
        root: Path,
        article: _ArticlePayload,
    ) -> ImageTextDownload:
        files, paths = self._download_images(root, article.images)
        replacements = {
            asset.markdown_url: file.path
            for asset, file in zip(article.images, files, strict=True)
            if asset.markdown_url is not None
        }
        markdown = article.markdown
        abstract = article.abstract
        for remote_url, local_path in replacements.items():
            markdown = markdown.replace(remote_url, local_path)
            if abstract is not None:
                abstract = abstract.replace(remote_url, local_path)
        text_path = root / "article.md"
        atomic_replace_bytes(text_path, (markdown.rstrip() + "\n").encode("utf-8"))
        manifest = ImageTextManifest(
            platform_id=aweme_id,
            source_input=source.input,
            variant="article",
            text_path="article.md",
            title=article.title,
            abstract=abstract,
            files=files,
        )
        manifest_path = root / "image_text.json"
        _write_manifest(manifest_path, manifest)
        return ImageTextDownload(
            manifest_path=manifest_path,
            files=tuple(paths),
            text_path=text_path,
        )

    def _download_images(
        self,
        root: Path,
        image_assets: Sequence[_ImageAsset],
    ) -> tuple[list[ImageTextFile], list[Path]]:
        files: list[ImageTextFile] = []
        paths: list[Path] = []
        for index, asset in enumerate(image_assets, start=1):
            payload = self._download_asset(asset.download_url)
            suffix = _image_suffix(asset.download_url)
            relative = Path("images") / f"image_{index:04d}{suffix}"
            destination = root / relative
            atomic_replace_bytes(destination, payload)
            digest = hashlib.sha256(payload).hexdigest()
            files.append(
                ImageTextFile(
                    path=relative.as_posix(),
                    bytes=len(payload),
                    sha256=digest,
                )
            )
            paths.append(destination)
        return files, paths

    def _download_asset(self, url: str) -> bytes:
        request = Request(
            url,
            headers={
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Referer": "https://www.douyin.com/",
            },
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = getattr(response, "status", None)
                if status is not None and not 200 <= int(status) < 300:
                    raise DouyinMediaError(
                        f"Douyin image request returned HTTP {int(status)}"
                    )
                payload = response.read()
        except DouyinMediaError:
            raise
        except HTTPError as error:
            raise DouyinMediaError(
                f"Douyin image request returned HTTP {error.code}"
            ) from error
        except (OSError, URLError) as error:
            raise DouyinMediaError("Douyin image request failed") from error
        if not payload:
            raise DouyinMediaError("Douyin image response was empty")
        if len(payload) > _MAX_IMAGE_BYTES:
            raise DouyinMediaError("Douyin image response exceeds 50 MB")
        return payload


def _aweme_id_from_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.douyin.com"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Douyin source must be a canonical work URL")
    prefix, _, selected = parsed.path.rstrip("/").rpartition("/video/")
    if prefix or not selected or not selected.isdigit():
        raise ValueError("Douyin source must be a canonical work URL")
    return selected


def _article_payload(detail: Mapping[str, Any]) -> _ArticlePayload | None:
    raw_article = detail.get("article_info")
    if not isinstance(raw_article, Mapping):
        return None
    content = _json_mapping(raw_article.get("article_content"))
    fe_data = _json_mapping(raw_article.get("fe_data"))
    markdown = content.get("markdown") if content is not None else None
    image_list = fe_data.get("image_list") if fe_data is not None else None
    article_type = str(raw_article.get("article_type"))
    if not isinstance(markdown, str) or not markdown.strip():
        if article_type == "999" or image_list:
            raise DouyinMediaError("Douyin article detail contains no markdown")
        return None
    return _ArticlePayload(
        title=_text(raw_article.get("article_title")),
        abstract=_text(
            content.get("long_article_abstract") if content is not None else None
        ),
        markdown=markdown,
        images=tuple(_article_image_assets(image_list)),
    )


def _gallery_image_urls(detail: Mapping[str, Any]) -> list[str]:
    raw_images = (
        detail.get("images") or detail.get("image_list") or detail.get("imageInfos")
    )
    if not isinstance(raw_images, Sequence) or isinstance(raw_images, (str, bytes)):
        return []
    urls: list[str] = []
    for image in raw_images:
        if not isinstance(image, Mapping):
            continue
        candidates = image.get("url_list") or image.get("download_url_list")
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
            continue
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            parsed = urlsplit(candidate.strip())
            if parsed.scheme == "https" and parsed.netloc:
                urls.append(candidate.strip())
                break
    return urls


def _article_image_assets(value: object) -> list[_ImageAsset]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    assets: list[_ImageAsset] = []
    for image in value:
        if not isinstance(image, Mapping):
            continue
        selected = next(
            (
                candidate
                for key in (
                    "origin_image_url",
                    "high_image_url",
                    "markdown_url",
                    "resize_url",
                    "same_url",
                )
                if (candidate := _https_url(image.get(key))) is not None
            ),
            None,
        )
        if selected is None:
            continue
        assets.append(
            _ImageAsset(
                download_url=selected,
                markdown_url=_https_url(image.get("markdown_url")),
            )
        )
    return assets


def _json_mapping(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _https_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    selected = value.strip()
    parsed = urlsplit(selected)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return selected


def _write_manifest(path: Path, manifest: ImageTextManifest) -> None:
    atomic_replace_bytes(
        path,
        (manifest.model_dump_json(indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )


def _text(value: object) -> str | None:
    if value is None:
        return None
    selected = str(value).strip()
    return selected or None


def _image_suffix(url: str) -> str:
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        return suffix
    return ".jpg"
