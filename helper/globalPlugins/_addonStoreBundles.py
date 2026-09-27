"""Add-on bundles: share and batch-install NVDA add-ons with a single file.

A bundle (``.nvda-bundle``) is a small JSON document listing add-ons by ID
with their download locations. It never contains the add-ons themselves,
so it stays tiny and email-friendly.

Each entry is either:

- ``latest``: resolved to the current version from the SerrebiRadio mirror
  catalog when the bundle is installed, or
- ``pinned``: locked to an exact version with its download URL and SHA-256,
  so installs are reproducible.

The format is versioned (``formatVersion``) so future variants, such as
fully-offline bundles with embedded files, can be added later.
"""

import hashlib
import json
import os
import tempfile
import urllib.request
from datetime import datetime, timezone

import wx

BUNDLE_EXTENSION = ".nvda-bundle"
BUNDLE_FORMAT = "nvda-addon-bundle"
BUNDLE_FORMAT_VERSION = 1
MIRROR_CATALOG_URL = "https://serrebidev.github.io/nvda-addon-mirror/addons.json"

MODE_LATEST = "latest"
MODE_PINNED = "pinned"

_DOWNLOAD_TIMEOUT = 30
_DOWNLOAD_CHUNK = 65536


class BundleError(Exception):
	"""Raised when a bundle file cannot be read or is invalid."""


def buildBundle(name, entries):
	"""Build the bundle document for *entries* (list of entry dicts)."""
	return {
		"format": BUNDLE_FORMAT,
		"formatVersion": BUNDLE_FORMAT_VERSION,
		"name": name,
		"created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
		"generator": "addonStoreMirror",
		"addons": list(entries),
	}


def makeEntry(*, addonId, displayName, installedVersion, mode, version=None, url=None, sha256=None):
	"""Build one bundle entry. Pinned entries carry version, url and sha256."""
	if mode not in (MODE_LATEST, MODE_PINNED):
		raise ValueError("Unknown bundle entry mode: %r" % (mode,))
	entry = {
		"addonId": addonId,
		"displayName": displayName,
		"installedVersion": installedVersion,
		"mode": mode,
	}
	if mode == MODE_PINNED:
		if not version or not url:
			raise ValueError("Pinned entries need a version and a URL")
		entry["version"] = version
		entry["url"] = url
		if sha256:
			entry["sha256"] = sha256
	return entry


def parseBundle(data):
	"""Validate a decoded JSON document and return it.

	:raise BundleError: if the document is not a bundle.
	"""
	if not isinstance(data, dict):
		raise BundleError(_("Not an add-on bundle: expected a JSON object."))
	if data.get("format") != BUNDLE_FORMAT:
		raise BundleError(_("Not an add-on bundle: unrecognized file format."))
	formatVersion = data.get("formatVersion")
	if not isinstance(formatVersion, int) or formatVersion < 1 or formatVersion > BUNDLE_FORMAT_VERSION:
		raise BundleError(_("Unsupported add-on bundle version: %s.") % (formatVersion,))
	addons = data.get("addons")
	if not isinstance(addons, list) or not addons:
		raise BundleError(_("This bundle does not list any add-ons."))
	for entry in addons:
		if not isinstance(entry, dict) or not entry.get("addonId"):
			raise BundleError(_("This bundle contains an invalid add-on entry."))
		mode = entry.get("mode", MODE_LATEST)
		if mode not in (MODE_LATEST, MODE_PINNED):
			raise BundleError(_("This bundle contains an invalid add-on entry."))
		if mode == MODE_PINNED and not (entry.get("version") and entry.get("url")):
			raise BundleError(_("This bundle contains an invalid add-on entry."))
	return data


def loadBundleFile(path):
	"""Read and validate a bundle file. :raise BundleError: on any problem."""
	try:
		with open(path, "r", encoding="utf-8") as f:
			data = json.load(f)
	except FileNotFoundError:
		raise BundleError(_("Could not open the bundle file."))
	except (OSError, ValueError):
		raise BundleError(_("Could not read the bundle file: it is not valid JSON."))
	return parseBundle(data)


def saveBundleFile(bundle, path):
	"""Write a bundle document to *path* as UTF-8 JSON."""
	with open(path, "w", encoding="utf-8") as f:
		json.dump(bundle, f, ensure_ascii=False, indent=2)
		f.write("\n")


def fetchCatalogMap(urlopen=urllib.request.urlopen):
	"""Fetch the mirror catalog and map addonId -> version info.

	Returns ``{addonId: {"displayName", "version", "url", "sha256"}}``.
	:raise OSError/ValueError: if the catalog cannot be fetched or parsed.
	"""
	with urlopen(MIRROR_CATALOG_URL, timeout=_DOWNLOAD_TIMEOUT) as response:
		data = json.loads(response.read().decode("utf-8"))
	addons = data.get("addons", data) if isinstance(data, dict) else data
	items = addons.values() if isinstance(addons, dict) else addons
	catalog = {}
	for item in items:
		addonId = item.get("addonId") or item.get("name")
		if not addonId:
			continue
		version = item.get("addonVersionName") or item.get("version")
		url = item.get("URL") or item.get("url")
		catalog[str(addonId)] = {
			"displayName": item.get("displayName") or item.get("summary") or str(addonId),
			"version": str(version) if version else "",
			"url": url or "",
			"sha256": item.get("sha256") or "",
		}
	return catalog


def getInstalledAddons(addonHandler):
	"""Return installed add-ons as sorted ``{"addonId", "displayName", "version"}`` dicts."""
	installed = []
	for addon in addonHandler.getAvailableAddons():
		try:
			manifest = addon.manifest
		except Exception:
			continue
		installed.append({
			"addonId": addon.name,
			"displayName": manifest.get("summary") or addon.name,
			"version": addon.version,
		})
	return sorted(installed, key=lambda item: item["displayName"].lower())


def buildExportEntries(installed, catalogMap, pinVersions):
	"""Build bundle entries for *installed* add-ons.

	With *pinVersions*, entries whose installed version matches the catalog's
	current version are pinned (exact URL + checksum); everything else falls
	back to ``latest`` mode.
	"""
	entries = []
	for addon in installed:
		info = catalogMap.get(addon["addonId"])
		if (
			pinVersions
			and info
			and info.get("version")
			and info["version"] == addon["version"]
			and info.get("url")
		):
			entries.append(makeEntry(
				addonId=addon["addonId"],
				displayName=addon["displayName"],
				installedVersion=addon["version"],
				mode=MODE_PINNED,
				version=info["version"],
				url=info["url"],
				sha256=info.get("sha256") or None,
			))
		else:
			entries.append(makeEntry(
				addonId=addon["addonId"],
				displayName=addon["displayName"],
				installedVersion=addon["version"],
				mode=MODE_LATEST,
			))
	return entries


def resolveEntries(bundle, catalogMap, installedMap):
	"""Resolve a bundle into an install plan for the import dialog.

	Returns a list of ``{"addonId", "displayName", "version", "url",
	"sha256", "source", "status", "installedVersion"}`` dicts. *status* is one
	of ``new``, ``update`` (installed version differs), ``up-to-date`` or
	``unavailable`` (no download location known).
	"""
	resolved = []
	for entry in bundle["addons"]:
		addonId = entry["addonId"]
		installed = installedMap.get(addonId)
		installedVersion = installed["version"] if installed else ""
		if entry.get("mode") == MODE_PINNED:
			version = entry.get("version", "")
			url = entry.get("url", "")
			sha256 = entry.get("sha256", "")
			source = _domainOf(url)
			available = bool(url)
		else:
			info = catalogMap.get(addonId, {})
			version = info.get("version", "")
			url = info.get("url", "")
			sha256 = info.get("sha256", "")
			source = _domainOf(url)
			available = bool(url)
		if not available:
			status = "unavailable"
		elif installed and installedVersion == version:
			status = "up-to-date"
		elif installed:
			status = "update"
		else:
			status = "new"
		resolved.append({
			"addonId": addonId,
			"displayName": entry.get("displayName") or catalogMap.get(addonId, {}).get("displayName") or addonId,
			"version": version,
			"url": url,
			"sha256": sha256,
			"source": source,
			"status": status,
			"installedVersion": installedVersion,
		})
	return resolved


def _domainOf(url):
	try:
		return urllib.request.urlparse(url).netloc or url
	except Exception:
		return url


def downloadToTemp(url, progress=None, urlopen=urllib.request.urlopen):
	"""Download *url* to a temp ``.nvda-addon`` file. Returns its path.

	*progress* is called as ``progress(downloadedBytes, totalBytes)`` where
	totalBytes may be 0 when unknown.
	"""
	fd, path = tempfile.mkstemp(suffix=".nvda-addon", prefix="nvda-bundle-")
	os.close(fd)
	try:
		with urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as response:
			total = int(response.headers.get("Content-Length", 0) or 0)
			downloaded = 0
			with open(path, "wb") as f:
				while True:
					chunk = response.read(_DOWNLOAD_CHUNK)
					if not chunk:
						break
					f.write(chunk)
					downloaded += len(chunk)
					if progress is not None:
						progress(downloaded, total)
	except Exception:
		try:
			os.remove(path)
		except OSError:
			pass
		raise
	return path


def sha256OfFile(path):
	digest = hashlib.sha256()
	with open(path, "rb") as f:
		for chunk in iter(lambda: f.read(_DOWNLOAD_CHUNK), b""):
			digest.update(chunk)
	return digest.hexdigest()


def installResolved(resolved, *, downloader=downloadToTemp, installer=None, progress=None):
	"""Download and install each resolved entry.

	*installer* defaults to NVDA's own add-on installer
	(``addonStore.install.installAddon``). *progress* is called as
	``progress(index, total, displayName, stage)`` with stage ``"download"``
	or ``"install"``. Returns ``{"installed": [...], "failed": [...],
	"skipped": [...]}`` with display names (failed items pair name + reason).
	"""
	if installer is None:
		installer = _nvdaInstaller
	installed, failed, skipped = [], [], []
	total = len(resolved)
	for index, item in enumerate(resolved):
		name = item["displayName"]
		if progress is not None:
			progress(index, total, name, "download")
		try:
			path = downloader(item["url"])
		except Exception as e:
			failed.append((name, _("Download failed: %s") % (e,)))
			continue
		try:
			expected = item.get("sha256")
			if expected and sha256OfFile(path).lower() != expected.lower():
				failed.append((name, _("Checksum mismatch: the download may be corrupted.")))
				continue
			if progress is not None:
				progress(index, total, name, "install")
			installer(path)
		except Exception as e:
			failed.append((name, str(e) or _("Installation failed.")))
		else:
			installed.append(name)
		finally:
			try:
				os.remove(path)
			except OSError:
				pass
	return {"installed": installed, "failed": failed, "skipped": skipped}


def _nvdaInstaller(path):
	"""Install one downloaded add-on file using NVDA's own installer."""
	from addonStore.install import installAddon
	installAddon(path)


class ExportBundleDialog(wx.Dialog):
	"""Choose installed add-ons and save them as a bundle file."""

	def __init__(self, parent, installed):
		# Translators: Export dialog title.
		super().__init__(parent, title=_("Export add-on bundle"))
		self._installed = installed
		sizer = wx.BoxSizer(wx.VERTICAL)
		# Translators: Export dialog instruction.
		label = wx.StaticText(self, label=_(
			"Select the add-ons to include in the bundle. The bundle is a small "
			"file with download links; it does not contain the add-ons themselves.",
		))
		label.Wrap(420)
		sizer.Add(label, 0, wx.ALL, 10)
		choices = [
			# Translators: {name} is the add-on name, {version} its version.
			_("{name} ({version})").format(name=item["displayName"], version=item["version"])
			for item in installed
		]
		self.addonList = wx.CheckListBox(self, choices=choices)
		for i in range(len(choices)):
			self.addonList.Check(i)
		sizer.Add(self.addonList, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
		# Translators: Radio box label for the version mode of a bundle export.
		self.modeRadio = wx.RadioBox(self, label=_("Versions to install"), choices=[
			# Translators: Bundle export mode: resolve the newest version when installing.
			_("Latest available"),
			# Translators: Bundle export mode: lock the currently installed versions.
			_("Pin to my installed versions"),
		])
		sizer.Add(self.modeRadio, 0, wx.EXPAND | wx.ALL, 10)
		nameSizer = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Label for the bundle name field.
		nameSizer.Add(wx.StaticText(self, label=_("Bundle name:")), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
		self.nameCtrl = wx.TextCtrl(self, value=_("NVDA add-ons"))
		nameSizer.Add(self.nameCtrl, 1, wx.EXPAND)
		sizer.Add(nameSizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
		btnSizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
		# Translators: Export button in the bundle export dialog.
		self.FindWindowById(wx.ID_OK).SetLabel(_("&Export"))
		sizer.Add(btnSizer, 0, wx.EXPAND | wx.ALL, 10)
		self.SetSizer(sizer)
		sizer.Fit(self)
		self.Bind(wx.EVT_BUTTON, self._onExport, id=wx.ID_OK)

	def _onExport(self, evt):
		selected = [self._installed[i] for i in range(len(self._installed)) if self.addonList.IsChecked(i)]
		if not selected:
			wx.MessageBox(
				# Translators: Shown when exporting a bundle with nothing selected.
				_("Select at least one add-on to export."),
				_("Export add-on bundle"),
				wx.OK | wx.ICON_INFORMATION,
			)
			return
		pinVersions = self.modeRadio.GetSelection() == 1
		try:
			catalogMap = fetchCatalogMap()
		except Exception:
			catalogMap = {}
		entries = buildExportEntries(selected, catalogMap, pinVersions)
		bundle = buildBundle(self.nameCtrl.GetValue().strip() or _("NVDA add-ons"), entries)
		# Translators: File dialog title and filter for saving a bundle.
		wildcard = _("NVDA add-on bundle (*%s)|*%s") % (BUNDLE_EXTENSION, BUNDLE_EXTENSION)
		with wx.FileDialog(
			self, _("Save add-on bundle"), wildcard=wildcard,
			style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
		) as fileDialog:
			fileDialog.SetFilename((bundle["name"] or "nvda-addons") + BUNDLE_EXTENSION)
			if fileDialog.ShowModal() != wx.ID_OK:
				return
			path = fileDialog.GetPath()
			if not path.lower().endswith(BUNDLE_EXTENSION):
				path += BUNDLE_EXTENSION
		try:
			saveBundleFile(bundle, path)
		except OSError as e:
			wx.MessageBox(
				_("Could not save the bundle: %s") % (e,),
				_("Export add-on bundle"),
				wx.OK | wx.ICON_ERROR,
			)
			return
		pinned = sum(1 for entry in entries if entry["mode"] == MODE_PINNED)
		wx.MessageBox(
			# Translators: {count} add-ons exported, {pinned} of them pinned.
			_("Exported {count} add-ons ({pinned} pinned to installed versions).").format(
				count=len(entries), pinned=pinned,
			),
			_("Export add-on bundle"),
			wx.OK | wx.ICON_INFORMATION,
		)
		self.EndModal(wx.ID_OK)


class ImportBundleDialog(wx.Dialog):
	"""Pick bundle entries and install them all at once."""

	def __init__(self, parent, bundle, catalogMap, installedMap):
		# Translators: Import dialog title.
		super().__init__(parent, title=_("Install from add-on bundle"))
		self._resolved = resolveEntries(bundle, catalogMap, installedMap)
		sizer = wx.BoxSizer(wx.VERTICAL)
		name = bundle.get("name") or _("Unnamed bundle")
		# Translators: Import dialog instruction; {name} is the bundle name.
		label = wx.StaticText(self, label=_(
			"Select the add-ons to install from the bundle \"{name}\". "
			"Only install bundles from people you trust.",
		).format(name=name))
		label.Wrap(420)
		sizer.Add(label, 0, wx.ALL, 10)
		choices = [self._labelFor(item) for item in self._resolved]
		self.addonList = wx.CheckListBox(self, choices=choices)
		for i, item in enumerate(self._resolved):
			# Pre-check anything not already up to date and available.
			self.addonList.Check(i, item["status"] in ("new", "update"))
		sizer.Add(self.addonList, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
		btnSizer = self.CreateButtonSizer(wx.OK | wx.CANCEL)
		# Translators: Install button in the bundle import dialog.
		self.FindWindowById(wx.ID_OK).SetLabel(_("&Install selected"))
		sizer.Add(btnSizer, 0, wx.EXPAND | wx.ALL, 10)
		self.SetSizer(sizer)
		sizer.Fit(self)
		self.Bind(wx.EVT_BUTTON, self._onInstall, id=wx.ID_OK)

	def _labelFor(self, item):
		status = item["status"]
		if status == "up-to-date":
			# Translators: {name} add-on name, {version} its version.
			detail = _("already installed ({version})").format(version=item["installedVersion"])
		elif status == "update":
			# Translators: {name} add-on name, {old} installed version, {new} bundle version.
			detail = _("installed: {old}, bundle: {new}").format(
				old=item["installedVersion"], new=item["version"] or _("unknown"),
			)
		elif status == "unavailable":
			# Translators: Shown when a bundle entry has no known download.
			detail = _("no download available")
		else:
			version = item["version"] or _("latest")
			detail = version
			if item["source"]:
				# Translators: {version} add-on version, {source} download host.
				detail = _("{version} from {source}").format(version=version, source=item["source"])
		return "%s (%s)" % (item["displayName"], detail)

	def _onInstall(self, evt):
		selected = [
			self._resolved[i]
			for i in range(len(self._resolved))
			if self.addonList.IsChecked(i)
		]
		available = [item for item in selected if item["status"] != "unavailable"]
		if not available:
			wx.MessageBox(
				# Translators: Shown when installing a bundle with nothing installable selected.
				_("Select at least one add-on with an available download."),
				_("Install from add-on bundle"),
				wx.OK | wx.ICON_INFORMATION,
			)
			return
		# Translators: Progress dialog title while installing a bundle.
		progress = wx.ProgressDialog(
			_("Installing add-ons"), _("Starting..."),
			maximum=len(available), parent=self,
			style=wx.PD_APP_MODAL | wx.PD_AUTO_HIDE,
		)

		def onProgress(index, total, displayName, stage):
			# Translators: {name} add-on name, {stage} download/install.
			stageLabel = _("downloading") if stage == "download" else _("installing")
			progress.Update(index, _("{name}: {stage}...").format(name=displayName, stage=stageLabel))

		try:
			result = installResolved(available, progress=onProgress)
		finally:
			progress.Destroy()
		lines = []
		if result["installed"]:
			# Translators: {count} add-ons installed.
			lines.append(_("Installed {count}: {names}.").format(
				count=len(result["installed"]), names=", ".join(result["installed"]),
			))
		for name, reason in result["failed"]:
			lines.append(_("{name}: {reason}").format(name=name, reason=reason))
		if not lines:
			lines.append(_("Nothing was installed."))
		wx.MessageBox(
			"\n".join(lines),
			_("Install from add-on bundle"),
			wx.OK | wx.ICON_INFORMATION,
		)
		if result["installed"]:
			import core
			# Translators: Asked after bundle installs complete.
			if wx.MessageBox(
				_("The installed add-ons need NVDA to restart. Restart now?"),
				_("Install from add-on bundle"),
				wx.YES_NO | wx.ICON_QUESTION,
			) == wx.YES:
				core.restart()
		self.EndModal(wx.ID_OK)
