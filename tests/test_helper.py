import builtins
import importlib.util
import itertools
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


HELPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "helper"
    / "globalPlugins"
    / "addonStoreMirror.py"
)


def _package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    return module


class _Log:
    def info(self, _message):
        pass

    def exception(self, _message):
        raise AssertionError(_message)


class _WxModule(types.ModuleType):
    """Fake wx module where every attribute is a unique int.

    Lets the plugin OR style flags together and compare event types, the way
    real wx constants behave. Tests assign real callables (e.g.
    GetTopLevelWindows, CheckBox) onto the instance when they need them;
    explicitly assigned attributes take precedence over generated ones.
    """

    def __init__(self, name):
        super().__init__(name)
        self._ids = itertools.count(1)

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        value = next(self._ids)
        setattr(self, name, value)
        return value


class HelperSourceSupportTests(unittest.TestCase):
    def _loadHelper(self, extraModules, addonStoreConf=None):
        config = types.ModuleType("config")

        class Conf(dict):
            spec = {}

        if addonStoreConf is None:
            addonStoreConf = {"baseServerURL": ""}
        config.conf = Conf(addonStore=addonStoreConf)
        self.config = config
        addonHandler = types.ModuleType("addonHandler")
        addonHandler.initTranslation = lambda: None
        globalPluginHandler = types.ModuleType("globalPluginHandler")
        globalPluginHandler.GlobalPlugin = object
        logHandler = types.ModuleType("logHandler")
        logHandler.log = _Log()
        wxModule = _WxModule("wx")
        self.wx = wxModule
        modules = {
            "addonHandler": addonHandler,
            "config": config,
            "globalPluginHandler": globalPluginHandler,
            "logHandler": logHandler,
            "wx": wxModule,
            **extraModules,
        }
        spec = importlib.util.spec_from_file_location(
            "addonStoreMirror_test",
            HELPER_PATH,
        )
        helper = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            builtins,
            "_",
            lambda text: text,
            create=True,
        ):
            spec.loader.exec_module(helper)
        return helper

    def test_current_nvda_models_list_column_search_and_restore(self):
        modelModule = types.ModuleType("addonStore.models.addon")

        class ModelBase:
            def asdict(self):
                return {"addonId": "example"}

        class Model(ModelBase):
            pass

        def createStoreModel(_data):
            return Model()

        def createInstalledStoreModel(_data):
            return Model()

        modelModule._AddonGUIModel = ModelBase
        modelModule._createStoreModelFromData = createStoreModel
        modelModule._createInstalledStoreModelFromData = createInstalledStoreModel

        listControlModule = types.ModuleType("gui.addonStoreGui.controls.addonList")

        class AddonVirtualList:
            def __init__(self, listViewModel):
                self._addonsListVM = listViewModel
                self.columns = []

            def _refreshColumns(self):
                self.columns = ["Name"]

            def GetColumnCount(self):
                return len(self.columns)

            def InsertColumn(self, _index, label, width):
                self.columns.append((label, width))

            def scaleSize(self, size):
                return size

            def OnGetItemText(self, _itemIndex, _colIndex):
                return "Example"

            def OnColClick(self, _event):
                raise AssertionError("Source click reached NVDA's sorter")

        listControlModule.AddonVirtualList = AddonVirtualList

        listViewModelModule = types.ModuleType("gui.addonStoreGui.viewModels.addonList")

        class AddonListItemVM:
            def __init__(self, model):
                self.model = model

            @property
            def searchableText(self):
                return "example addon"

        class AddonListVM:
            presentedFields = ("name",)

            def __init__(self, item):
                self.item = item

            def getAddonAtIndex(self, _index):
                return self.item

        listViewModelModule.AddonListItemVM = AddonListItemVM
        listViewModelModule.AddonListVM = AddonListVM

        modules = {
            "addonStore": _package("addonStore"),
            "addonStore.models": _package("addonStore.models"),
            "addonStore.models.addon": modelModule,
            "gui": _package("gui"),
            "gui.addonStoreGui": _package("gui.addonStoreGui"),
            "gui.addonStoreGui.controls": _package("gui.addonStoreGui.controls"),
            "gui.addonStoreGui.controls.addonList": listControlModule,
            "gui.addonStoreGui.viewModels": _package("gui.addonStoreGui.viewModels"),
            "gui.addonStoreGui.viewModels.addonList": listViewModelModule,
        }
        helper = self._loadHelper(modules)
        plugin = helper.GlobalPlugin.__new__(helper.GlobalPlugin)
        plugin._sourceSupportPatches = []

        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            builtins,
            "_",
            lambda text: text,
            create=True,
        ):
            plugin._enableSourceSupport()

            model = modelModule._createStoreModelFromData(
                {"storeSource": "NV Access Add-on Store"},
            )
            item = listViewModelModule.AddonListItemVM(model)
            listViewModel = AddonListVM(item)
            control = listControlModule.AddonVirtualList(listViewModel)
            control._refreshColumns()

            self.assertEqual(("Source", 140), control.columns[-1])
            self.assertEqual(
                "NV Access Add-on Store",
                control.OnGetItemText(0, 1),
            )
            self.assertIn("nv access add-on store", item.searchableText)
            self.assertEqual(
                "NV Access Add-on Store",
                model.asdict()["storeSource"],
            )

            event = types.SimpleNamespace(GetColumn=lambda: 1)
            self.assertIsNone(control.OnColClick(event))

            plugin._restoreSourceSupport()

        self.assertIs(modelModule._createStoreModelFromData, createStoreModel)
        self.assertIs(
            modelModule._createInstalledStoreModelFromData,
            createInstalledStoreModel,
        )
        self.assertEqual(AddonVirtualList._refreshColumns.__name__, "_refreshColumns")


class HelperNvdaFloorTests(unittest.TestCase):
    """NVDA gained [addonStore] baseServerURL in 2025.1.

    2023.2 through 2024.4 hardcode addonStore.network.BASE_URL, so nothing can
    redirect their Add-on Store. The helper used to read the missing key only
    after installing its patches, so KeyError escaped __init__ with the Add-on
    Store GUI already modified -- and because the plugin object was then
    discarded, terminate() never ran to undo it.
    """

    _loadHelper = HelperSourceSupportTests._loadHelper

    def test_older_nvda_is_reported_and_left_untouched(self):
        errors = []

        class Log:
            def info(self, _message):
                pass

            def error(self, message):
                errors.append(message)

            def exception(self, _message):
                raise AssertionError(_message)

        helper = self._loadHelper({}, addonStoreConf={"showWarning": True})
        helper.log = Log()

        plugin = helper.GlobalPlugin.__new__(helper.GlobalPlugin)
        helper.GlobalPlugin.__init__(plugin)

        self.assertEqual([], plugin._sourceSupportPatches)
        self.assertFalse(plugin._urlApplied)
        self.assertEqual(1, len(errors))
        self.assertIn("2025.1", errors[0])

        # terminate() must not write a store URL it never replaced.
        plugin.terminate()
        self.assertNotIn("baseServerURL", self.config.conf["addonStore"])

    def test_manifest_requires_nvda_2025_1(self):
        manifest = (
            Path(__file__).resolve().parents[1] / "helper" / "manifest.ini"
        ).read_text(encoding="utf-8")

        self.assertIn("minimumNVDAVersion = 2025.1.0", manifest)


class _FakeEvent:
    def __init__(self, eventType, keyCode=None):
        self._eventType = eventType
        self._keyCode = keyCode
        self.skipped = False

    def GetEventType(self):
        return self._eventType

    def GetKeyCode(self):
        return self._keyCode

    def Skip(self):
        self.skipped = True


class HelperDeferredSearchTests(unittest.TestCase):
    """Deferred search: with "search while typing" off, keystrokes are ignored
    and the pending filter text is applied when Enter is pressed."""

    _loadHelper = HelperSourceSupportTests._loadHelper

    def _makeDialogPlugin(self, searchAsYouType):
        class FakeSearchCtrl:
            def __init__(self):
                self.binds = []

            def Bind(self, event, handler):
                self.binds.append((event, handler))

        class FakeDialog:
            def __init__(self):
                self.searchFilterCtrl = FakeSearchCtrl()
                self.filterCalls = []
                self.createFilterArgs = None

            def _createFilterControls(self, *args, **kwargs):
                self.createFilterArgs = (args, kwargs)
                self.searchFilterCtrl = FakeSearchCtrl()

            def onFilterTextChange(self, evt):
                self.filterCalls.append(evt)

        storeDialogModule = types.ModuleType("gui.addonStoreGui.controls.storeDialog")
        storeDialogModule.AddonStoreDialog = FakeDialog
        helper = self._loadHelper(
            {"gui.addonStoreGui.controls.storeDialog": storeDialogModule},
        )
        self.config.conf["serrebiStore"] = {"searchAsYouType": searchAsYouType}
        plugin = helper.GlobalPlugin.__new__(helper.GlobalPlugin)
        plugin._sourceSupportPatches = []
        modules = {
            "wx": self.wx,
            "config": self.config,
            "gui.addonStoreGui.controls.storeDialog": storeDialogModule,
        }
        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            builtins, "_", lambda text: text, create=True,
        ):
            plugin._enableDeferredSearch()
        dialog = FakeDialog()
        # Recent NVDA passes the sizer helper; this is the call that raised
        # TypeError on NVDA alpha before the wrapper forwarded arguments.
        dialog._createFilterControls("SIZER")
        return helper, plugin, dialog

    def _keyHandlers(self, dialog):
        return [
            handler
            for event, handler in dialog.searchFilterCtrl.binds
            if event is self.wx.EVT_KEY_DOWN
        ]

    def test_create_filter_controls_forwards_sizer_argument(self):
        _helper, _plugin, dialog = self._makeDialogPlugin(searchAsYouType=False)
        self.assertEqual((("SIZER",), {}), dialog.createFilterArgs)

    def test_key_handler_bound_to_search_field(self):
        _helper, _plugin, dialog = self._makeDialogPlugin(searchAsYouType=False)
        self.assertEqual(1, len(self._keyHandlers(dialog)))

    def test_keystrokes_ignored_until_enter_when_setting_off(self):
        _helper, _plugin, dialog = self._makeDialogPlugin(searchAsYouType=False)

        textEvt = _FakeEvent(self.wx.wxEVT_TEXT)
        dialog.onFilterTextChange(textEvt)
        self.assertEqual([], dialog.filterCalls)
        self.assertTrue(textEvt.skipped)

        enterEvt = _FakeEvent(self.wx.EVT_KEY_DOWN, keyCode=self.wx.WXK_RETURN)
        self._keyHandlers(dialog)[0](enterEvt)
        self.assertEqual([enterEvt], dialog.filterCalls)
        # Enter must not propagate to the dialog's default button.
        self.assertFalse(enterEvt.skipped)

    def test_numpad_enter_also_applies_filter(self):
        _helper, _plugin, dialog = self._makeDialogPlugin(searchAsYouType=False)
        enterEvt = _FakeEvent(
            self.wx.EVT_KEY_DOWN, keyCode=self.wx.WXK_NUMPAD_ENTER,
        )
        self._keyHandlers(dialog)[0](enterEvt)
        self.assertEqual([enterEvt], dialog.filterCalls)

    def test_other_keys_pass_through(self):
        _helper, _plugin, dialog = self._makeDialogPlugin(searchAsYouType=False)
        keyEvt = _FakeEvent(self.wx.EVT_KEY_DOWN, keyCode=self.wx.WXK_A)
        self._keyHandlers(dialog)[0](keyEvt)
        self.assertTrue(keyEvt.skipped)
        self.assertEqual([], dialog.filterCalls)

    def test_keystrokes_filter_immediately_when_setting_on(self):
        _helper, _plugin, dialog = self._makeDialogPlugin(searchAsYouType=True)

        textEvt = _FakeEvent(self.wx.wxEVT_TEXT)
        dialog.onFilterTextChange(textEvt)
        self.assertEqual([textEvt], dialog.filterCalls)

        enterEvt = _FakeEvent(self.wx.EVT_KEY_DOWN, keyCode=self.wx.WXK_RETURN)
        self._keyHandlers(dialog)[0](enterEvt)
        self.assertTrue(enterEvt.skipped)
        self.assertEqual([textEvt], dialog.filterCalls)

    def test_restore_returns_original_filter_behavior(self):
        _helper, plugin, dialog = self._makeDialogPlugin(searchAsYouType=False)
        modules = {"wx": self.wx, "config": self.config}
        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            builtins, "_", lambda text: text, create=True,
        ):
            plugin._restoreSourceSupport()
        textEvt = _FakeEvent(self.wx.wxEVT_TEXT)
        dialog.onFilterTextChange(textEvt)
        self.assertEqual([textEvt], dialog.filterCalls)
        self.assertEqual([], plugin._sourceSupportPatches)


class HelperDuplicateWarningTests(unittest.TestCase):
    """Duplicate installs: warn instead of silently dropping repeated add-ons."""

    _loadHelper = HelperSourceSupportTests._loadHelper

    def _makeStorePlugin(self):
        state = {"answer": None}
        messages = []

        class FakeVM:
            def __init__(self, addonId, displayName):
                self.Id = addonId
                self.model = types.SimpleNamespace(displayName=displayName)

        class FakeStoreVM:
            calls = []

            @classmethod
            def getAddons(cls, listItemVMs, *args, **kwargs):
                cls.calls.append((list(listItemVMs), args, kwargs))

        storeModule = types.ModuleType("gui.addonStoreGui.viewModels.store")
        storeModule.AddonStoreVM = FakeStoreVM

        class FakeGui(types.ModuleType):
            def messageBox(self, message, caption, style):
                messages.append((message, caption, style))
                return state["answer"]

        fakeGui = FakeGui("gui")
        helper = self._loadHelper(
            {
                "gui": fakeGui,
                "gui.addonStoreGui.viewModels.store": storeModule,
            }
        )
        plugin = helper.GlobalPlugin.__new__(helper.GlobalPlugin)
        plugin._sourceSupportPatches = []
        originalGetAddons = FakeStoreVM.__dict__["getAddons"]
        modules = {
            "wx": self.wx,
            "gui": fakeGui,
            "gui.addonStoreGui.viewModels.store": storeModule,
        }
        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            builtins, "_", lambda text: text, create=True,
        ):
            plugin._enableDuplicateInstallWarning()
        return helper, plugin, FakeStoreVM, FakeVM, messages, modules, state, originalGetAddons

    def _callGetAddons(self, storeVM, vms, modules):
        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            builtins, "_", lambda text: text, create=True,
        ):
            storeVM.getAddons(vms)

    def test_unique_selection_passes_through_without_prompt(self):
        _helper, plugin, StoreVM, VM, messages, modules, _state, _orig = (
            self._makeStorePlugin()
        )
        vms = [VM("a", "Alpha"), VM("b", "Beta")]
        self._callGetAddons(StoreVM, vms, modules)
        self.assertEqual([], messages)
        self.assertEqual(1, len(StoreVM.calls))
        self.assertEqual(vms, StoreVM.calls[0][0])

    def test_duplicate_yes_installs_first_of_each(self):
        _helper, plugin, StoreVM, VM, messages, modules, state, _orig = (
            self._makeStorePlugin()
        )
        state["answer"] = self.wx.ID_YES
        vms = [VM("a", "Alpha"), VM("a", "Alpha"), VM("b", "Beta")]
        self._callGetAddons(StoreVM, vms, modules)
        self.assertEqual(1, len(messages))
        self.assertIn("Alpha", messages[0][0])
        passed = StoreVM.calls[0][0]
        self.assertEqual(["a", "b"], [vm.Id for vm in passed])
        self.assertIs(vms[0], passed[0])

    def test_duplicate_no_aborts_install(self):
        _helper, plugin, StoreVM, VM, messages, modules, state, _orig = (
            self._makeStorePlugin()
        )
        state["answer"] = self.wx.ID_NO
        vms = [VM("a", "Alpha"), VM("a", "Alpha")]
        self._callGetAddons(StoreVM, vms, modules)
        self.assertEqual(1, len(messages))
        self.assertEqual([], StoreVM.calls)

    def test_getaddons_stays_a_classmethod_and_restores_exactly(self):
        _helper, plugin, StoreVM, _VM, _messages, modules, _state, original = (
            self._makeStorePlugin()
        )
        self.assertIsInstance(StoreVM.__dict__["getAddons"], classmethod)
        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            builtins, "_", lambda text: text, create=True,
        ):
            plugin._restoreSourceSupport()
        self.assertIs(original, StoreVM.__dict__["getAddons"])
        self.assertEqual([], plugin._sourceSupportPatches)


class HelperToolsMenuTests(unittest.TestCase):
    """Tools menu: one extra entry for NVDA's official Add-on Store.

    The regular Add-on Store menu option already opens the SerrebiRadio
    mirror, so only the official store needs its own entry.
    """

    _loadHelper = HelperSourceSupportTests._loadHelper

    def _makeMenuPlugin(self):
        class FakeToolsMenu:
            def __init__(self):
                self.items = []
                self.removed = []

            def Append(self, _id, label):
                item = ("item", label)
                self.items.append(item)
                return item

            def Remove(self, item):
                self.removed.append(item)
                return item

        class FakeSysTrayIcon:
            def __init__(self):
                self.toolsMenu = FakeToolsMenu()
                self.binds = []

            def Bind(self, event, handler, source=None):
                self.binds.append((event, handler, source))

        mainFrame = types.SimpleNamespace(sysTrayIcon=FakeSysTrayIcon())
        gui = types.ModuleType("gui")
        gui.mainFrame = mainFrame
        helper = self._loadHelper({"gui": gui})
        plugin = helper.GlobalPlugin.__new__(helper.GlobalPlugin)
        plugin._sourceSupportPatches = []
        plugin._toolsMenuItems = []
        modules = {"wx": self.wx, "gui": gui}
        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            builtins, "_", lambda text: text, create=True,
        ):
            plugin._addToolsMenuItems()
        return helper, plugin, gui, modules

    def test_official_store_item_added(self):
        _helper, _plugin, gui, _modules = self._makeMenuPlugin()
        menu = gui.mainFrame.sysTrayIcon.toolsMenu
        self.assertEqual(1, len(menu.items))
        _item, label = menu.items[0]
        self.assertIn("official", label)
        binds = gui.mainFrame.sysTrayIcon.binds
        self.assertEqual(1, len(binds))
        event, _handler, _source = binds[0]
        self.assertIs(self.wx.EVT_MENU, event)

    def test_menu_handler_opens_official_store(self):
        helper, plugin, _gui, _modules = self._makeMenuPlugin()
        opened = []
        plugin._openStore = lambda url, restoreURL: opened.append((url, restoreURL))
        for _event, handler, _source in _gui.mainFrame.sysTrayIcon.binds:
            handler(None)
        self.assertEqual(
            [(helper.OFFICIAL_STORE_URL, helper.MIRROR_STORE_URL)], opened,
        )

    def test_remove_menu_items(self):
        _helper, plugin, gui, modules = self._makeMenuPlugin()
        menu = gui.mainFrame.sysTrayIcon.toolsMenu
        with mock.patch.dict(sys.modules, modules):
            plugin._removeToolsMenuItems()
        self.assertEqual(1, len(menu.removed))
        self.assertEqual([], plugin._toolsMenuItems)


class HelperOpenStoreTests(unittest.TestCase):
    """_openStore: switch the store URL for one dialog, restore it on close."""

    _loadHelper = HelperSourceSupportTests._loadHelper

    def _makeOpenStorePlugin(self):
        calls = []
        topWindows = []

        class FakeDialog:
            instances = []

            def __init__(self, parent, storeVM):
                self.parent = parent
                self.storeVM = storeVM
                self.binds = []
                self.shown = False
                self.raised = False
                self.focused = False
                FakeDialog.instances.append(self)

            def Bind(self, event, handler):
                self.binds.append((event, handler))

            def Show(self):
                self.shown = True

            def Raise(self):
                self.raised = True

            def SetFocus(self):
                self.focused = True

        class FakeStoreVM:
            def __init__(self):
                self.refreshed = False

            def refresh(self):
                self.refreshed = True

        class MultiInstanceErrorWithDialog(Exception):
            def __init__(self, dialog):
                super().__init__()
                self.dialog = dialog

        class FakeSettingsDialog:
            pass

        FakeSettingsDialog.MultiInstanceErrorWithDialog = MultiInstanceErrorWithDialog

        mainFrame = types.SimpleNamespace(
            prePopup=lambda: calls.append("prePopup"),
            postPopup=lambda: calls.append("postPopup"),
        )
        gui = types.ModuleType("gui")
        gui.mainFrame = mainFrame
        gui.SettingsDialog = FakeSettingsDialog
        addonStoreGui = types.ModuleType("gui.addonStoreGui")
        addonStoreGui.AddonStoreDialog = FakeDialog
        storeModels = types.ModuleType("gui.addonStoreGui.viewModels.store")
        storeModels.AddonStoreVM = FakeStoreVM
        # _refreshStore looks up the data manager singleton; None means
        # "nothing to refresh" and keeps the close restorer quiet.
        addonStorePkg = types.ModuleType("addonStore")
        dataManagerMod = types.ModuleType("addonStore.dataManager")
        dataManagerMod.addonDataManager = None

        helper = self._loadHelper(
            {
                "gui": gui,
                "gui.addonStoreGui": addonStoreGui,
                "gui.addonStoreGui.viewModels.store": storeModels,
            }
        )
        self.wx.GetTopLevelWindows = lambda: topWindows
        plugin = helper.GlobalPlugin.__new__(helper.GlobalPlugin)
        plugin._sourceSupportPatches = []
        modules = {
            "wx": self.wx,
            "config": self.config,
            "gui": gui,
            "gui.addonStoreGui": addonStoreGui,
            "gui.addonStoreGui.viewModels.store": storeModels,
            "addonStore": addonStorePkg,
            "addonStore.dataManager": dataManagerMod,
        }
        fakes = {
            "dialogClass": FakeDialog,
            "calls": calls,
            "topWindows": topWindows,
        }
        return helper, plugin, fakes, modules

    def _patched(self, modules):
        return (
            mock.patch.dict(sys.modules, modules),
            mock.patch.object(builtins, "_", lambda text: text, create=True),
        )

    def test_open_official_store_restores_mirror_on_close(self):
        helper, plugin, fakes, modules = self._makeOpenStorePlugin()
        self.config.conf["addonStore"] = {"baseServerURL": helper.MIRROR_STORE_URL}
        FakeDialog = fakes["dialogClass"]
        with self._patched(modules)[0], self._patched(modules)[1]:
            plugin._openStore(helper.OFFICIAL_STORE_URL, restoreURL=helper.MIRROR_STORE_URL)
        self.assertEqual("", self.config.conf["addonStore"]["baseServerURL"])
        self.assertEqual(1, len(FakeDialog.instances))
        dialog = FakeDialog.instances[0]
        self.assertTrue(dialog.shown)
        self.assertTrue(dialog.storeVM.refreshed)
        closeHandlers = [
            handler for event, handler in dialog.binds if event is self.wx.EVT_CLOSE
        ]
        self.assertEqual(1, len(closeHandlers))

        closeEvt = _FakeEvent(self.wx.EVT_CLOSE)
        with mock.patch.dict(sys.modules, modules):
            closeHandlers[0](closeEvt)
        self.assertEqual(
            helper.MIRROR_STORE_URL, self.config.conf["addonStore"]["baseServerURL"]
        )
        self.assertTrue(closeEvt.skipped)
        self.assertEqual(["prePopup", "postPopup"], fakes["calls"])

    def test_open_mirror_store_binds_no_close_restorer(self):
        helper, plugin, fakes, modules = self._makeOpenStorePlugin()
        self.config.conf["addonStore"] = {"baseServerURL": ""}
        FakeDialog = fakes["dialogClass"]
        with self._patched(modules)[0], self._patched(modules)[1]:
            plugin._openStore(helper.MIRROR_STORE_URL, restoreURL=None)
        self.assertEqual(
            helper.MIRROR_STORE_URL, self.config.conf["addonStore"]["baseServerURL"]
        )
        dialog = FakeDialog.instances[0]
        self.assertEqual(
            [],
            [handler for event, handler in dialog.binds if event is self.wx.EVT_CLOSE],
        )

    def test_existing_dialog_is_focused_not_reopened(self):
        helper, plugin, fakes, modules = self._makeOpenStorePlugin()
        FakeDialog = fakes["dialogClass"]
        existing = FakeDialog(None, None)
        self.wx.GetTopLevelWindows = lambda: [existing]
        self.config.conf["addonStore"] = {"baseServerURL": "SENTINEL"}
        with self._patched(modules)[0], self._patched(modules)[1]:
            plugin._openStore(helper.OFFICIAL_STORE_URL, restoreURL=helper.MIRROR_STORE_URL)
        self.assertEqual("SENTINEL", self.config.conf["addonStore"]["baseServerURL"])
        self.assertTrue(existing.raised)
        self.assertTrue(existing.focused)
        self.assertEqual(1, len(FakeDialog.instances))


class HelperSettingsPanelTests(unittest.TestCase):
    _loadHelper = HelperSourceSupportTests._loadHelper

    def test_register_and_unregister(self):
        settingsDialogs = types.ModuleType("gui.settingsDialogs")
        categoryClasses = []
        settingsDialogs.NVDASettingsDialog = types.SimpleNamespace(
            categoryClasses=categoryClasses,
        )
        gui = types.ModuleType("gui")
        gui.settingsDialogs = settingsDialogs
        helper = self._loadHelper(
            {"gui": gui, "gui.settingsDialogs": settingsDialogs},
        )
        plugin = helper.GlobalPlugin.__new__(helper.GlobalPlugin)
        plugin._settingsPanelRegistered = False
        modules = {"gui": gui, "gui.settingsDialogs": settingsDialogs}
        with mock.patch.dict(sys.modules, modules):
            plugin._registerSettingsPanel()
        self.assertIn(helper.SerrebiStoreSettingsPanel, categoryClasses)
        self.assertTrue(plugin._settingsPanelRegistered)
        with mock.patch.dict(sys.modules, modules):
            plugin._registerSettingsPanel()
        self.assertEqual(1, len(categoryClasses))
        with mock.patch.dict(sys.modules, modules):
            plugin._unregisterSettingsPanel()
        self.assertEqual([], categoryClasses)
        self.assertFalse(plugin._settingsPanelRegistered)

    def test_makeSettings_and_onSave_round_trip(self):
        helper = self._loadHelper({})
        self.assertEqual(
            "SerrebiRadio add-on store", helper.SerrebiStoreSettingsPanel.title,
        )

        class FakeCheckBox:
            def __init__(self, parent, label):
                self.label = label
                self.value = None

            def SetValue(self, value):
                self.value = value

            def IsChecked(self):
                return self.value

        self.wx.CheckBox = FakeCheckBox
        created = {}

        class FakeSizer:
            def addItem(self, item):
                created["checkbox"] = item
                return item

        panel = helper.SerrebiStoreSettingsPanel()
        self.config.conf["serrebiStore"] = {"searchAsYouType": False}
        modules = {"wx": self.wx, "config": self.config}
        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            builtins, "_", lambda text: text, create=True,
        ):
            panel.makeSettings(FakeSizer())
        self.assertFalse(created["checkbox"].value)
        created["checkbox"].value = True
        with mock.patch.dict(sys.modules, modules):
            panel.onSave()
        self.assertTrue(self.config.conf["serrebiStore"]["searchAsYouType"])


class HelperInitTerminateTests(unittest.TestCase):
    """Full __init__/terminate wiring with every collaborator faked."""

    _loadHelper = HelperSourceSupportTests._loadHelper

    def _fullFakes(self):
        modelModule = types.ModuleType("addonStore.models.addon")

        def factory(_data):
            return types.SimpleNamespace(asdict=lambda: {})

        modelModule._createStoreModelFromData = factory
        modelModule._createInstalledStoreModelFromData = factory

        class ModelBase:
            def asdict(self):
                return {}

        modelModule._AddonGUIModel = ModelBase

        listControlModule = types.ModuleType("gui.addonStoreGui.controls.addonList")

        class AddonVirtualList:
            def _refreshColumns(self):
                pass

            def OnGetItemText(self, _item, _col):
                return ""

            def OnColClick(self, _event):
                return None

        listControlModule.AddonVirtualList = AddonVirtualList

        listViewModelModule = types.ModuleType("gui.addonStoreGui.viewModels.addonList")

        class AddonListItemVM:
            pass

        class AddonListVM:
            def _getFilteredSortedIds(self):
                return []

        listViewModelModule.AddonListItemVM = AddonListItemVM
        listViewModelModule.AddonListVM = AddonListVM

        storeDialogModule = types.ModuleType("gui.addonStoreGui.controls.storeDialog")

        class AddonStoreDialog:
            def _createFilterControls(self):
                pass

            def onFilterTextChange(self, _evt):
                pass

        storeDialogModule.AddonStoreDialog = AddonStoreDialog

        storeModule = types.ModuleType("gui.addonStoreGui.viewModels.store")

        class AddonStoreVM:
            @classmethod
            def getAddons(cls, listItemVMs, *args, **kwargs):
                pass

        storeModule.AddonStoreVM = AddonStoreVM

        addonStorePkg = types.ModuleType("addonStore")
        dataManagerMod = types.ModuleType("addonStore.dataManager")
        dataManagerMod.addonDataManager = None

        class FakeToolsMenu:
            def __init__(self):
                self.items = []
                self.removed = []

            def Append(self, _id, label):
                item = ("item", label)
                self.items.append(item)
                return item

            def Remove(self, item):
                self.removed.append(item)

        class FakeSysTrayIcon:
            def __init__(self):
                self.toolsMenu = FakeToolsMenu()

            def Bind(self, _event, _handler, _source=None):
                pass

        settingsDialogs = types.ModuleType("gui.settingsDialogs")
        settingsDialogs.NVDASettingsDialog = types.SimpleNamespace(categoryClasses=[])
        gui = types.ModuleType("gui")
        gui.mainFrame = types.SimpleNamespace(sysTrayIcon=FakeSysTrayIcon())
        gui.settingsDialogs = settingsDialogs

        return {
            "addonStore": addonStorePkg,
            "addonStore.dataManager": dataManagerMod,
            "addonStore.models.addon": modelModule,
            "gui": gui,
            "gui.settingsDialogs": settingsDialogs,
            "gui.addonStoreGui.controls.addonList": listControlModule,
            "gui.addonStoreGui.viewModels.addonList": listViewModelModule,
            "gui.addonStoreGui.controls.storeDialog": storeDialogModule,
            "gui.addonStoreGui.viewModels.store": storeModule,
            "wx": self.wx,
            "config": self.config,
        }

    def test_init_wires_everything_and_terminate_unwinds(self):
        helper = self._loadHelper({})
        fakes = self._fullFakes()
        plugin = helper.GlobalPlugin.__new__(helper.GlobalPlugin)
        # NVDA's config spec would pre-populate these defaults.
        self.config.conf["serrebiStore"] = {
            "originalStoreURL": "",
            "searchAsYouType": True,
        }
        with mock.patch.dict(sys.modules, fakes), mock.patch.object(
            builtins, "_", lambda text: text, create=True,
        ):
            helper.GlobalPlugin.__init__(plugin)

        self.assertTrue(plugin._urlApplied)
        self.assertEqual(
            helper.MIRROR_STORE_URL, self.config.conf["addonStore"]["baseServerURL"],
        )
        self.assertEqual(
            "", self.config.conf["serrebiStore"]["originalStoreURL"],
        )
        menu = fakes["gui"].mainFrame.sysTrayIcon.toolsMenu
        self.assertEqual(1, len(menu.items))
        self.assertIn(
            helper.SerrebiStoreSettingsPanel,
            fakes["gui.settingsDialogs"].NVDASettingsDialog.categoryClasses,
        )
        self.assertTrue(plugin._settingsPanelRegistered)
        self.assertTrue(plugin._sourceSupportPatches)

        with mock.patch.dict(sys.modules, fakes), mock.patch.object(
            builtins, "_", lambda text: text, create=True,
        ):
            plugin.terminate()

        self.assertEqual("", self.config.conf["addonStore"]["baseServerURL"])
        self.assertEqual(1, len(menu.removed))
        self.assertEqual([], plugin._toolsMenuItems)
        self.assertEqual(
            [],
            fakes["gui.settingsDialogs"].NVDASettingsDialog.categoryClasses,
        )
        self.assertFalse(plugin._settingsPanelRegistered)
        self.assertEqual([], plugin._sourceSupportPatches)


if __name__ == "__main__":
    unittest.main()
