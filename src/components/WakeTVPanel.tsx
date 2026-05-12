import {
  ButtonItem,
  DialogButton,
  PanelSection,
  PanelSectionRow,
  Field,
  TextField,
  ToggleField,
  Spinner,
} from "@decky/ui";
import { call } from "@decky/api";
import { FC, CSSProperties, useEffect, useState, useCallback } from "react";
import {
  FaPowerOff, FaPlug, FaSave,
  FaCircle, FaChevronRight,
} from "react-icons/fa";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface Settings {
  tv_ip: string;
  hdmi_input: string;
  mac_address: string;
  paired: boolean;
  wake_on_guide_button: boolean;
  wake_on_resume: boolean;
}

interface OkResult {
  ok: boolean;
  error?: string;
  note?: string;
  mac_address?: string;
}

// ---------------------------------------------------------------------------
// HDMI options
// ---------------------------------------------------------------------------

const HDMI_INPUTS = ["HDMI_1", "HDMI_2", "HDMI_3", "HDMI_4"];
const hdmiLabel = (val: string) => val.replace("_", " ");

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const statusDot: CSSProperties = {
  display: "inline-block",
  fontSize: "10px",
  marginRight: "6px",
};

const statusBar: CSSProperties = {
  display: "flex",
  alignItems: "center",
  padding: "8px 12px",
  borderRadius: "8px",
  background: "rgba(255,255,255,0.04)",
  marginBottom: "4px",
};

const feedbackText: CSSProperties = {
  fontSize: "12px",
  marginTop: "4px",
  minHeight: "18px",
};

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

export const WakeTVPanel: FC = () => {
  const [tvIp, setTvIp] = useState("");
  const [hdmiInput, setHdmiInput] = useState("HDMI_1");
  const [macAddress, setMacAddress] = useState("");
  const [paired, setPaired] = useState(false);
  const [reachable, setReachable] = useState(false);
  const [wakeOnGuide, setWakeOnGuide] = useState(true);
  const [wakeOnResume, setWakeOnResume] = useState(true);

  const [busy, setBusy] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);

  const [pollVersion, setPollVersion] = useState(0);

  useEffect(() => {
    call<[], Settings>("get_settings")
      .then((s) => {
        setTvIp(s.tv_ip);
        setHdmiInput(s.hdmi_input);
        setMacAddress(s.mac_address);
        setPaired(s.paired);
        setWakeOnGuide(s.wake_on_guide_button);
        setWakeOnResume(s.wake_on_resume);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    let active = true;
    const check = () => {
      call<[], { reachable: boolean }>("get_status")
        .then((s) => { if (active) setReachable(s.reachable); })
        .catch(() => { if (active) setReachable(false); });
    };
    check();
    const id = setInterval(check, 15_000);
    return () => { active = false; clearInterval(id); };
  }, [pollVersion]);

  const showFeedback = useCallback((msg: string) => {
    setFeedback(msg);
    setTimeout(() => setFeedback(null), 5000);
  }, []);

  const cycleHdmi = useCallback(() => {
    setHdmiInput((prev) => {
      const idx = HDMI_INPUTS.indexOf(prev);
      return HDMI_INPUTS[(idx + 1) % HDMI_INPUTS.length];
    });
  }, []);

  const handleSave = useCallback(async () => {
    setBusy("save");
    try {
      await call<[string, string, string, boolean, boolean], OkResult>(
        "save_settings", tvIp, hdmiInput, macAddress, wakeOnGuide, wakeOnResume
      );
      setPollVersion((v) => v + 1);
      showFeedback("Settings saved");
    } catch {
      showFeedback("Failed to save");
    } finally {
      setBusy(null);
    }
  }, [tvIp, hdmiInput, macAddress, wakeOnGuide, wakeOnResume, showFeedback]);

  const handlePair = useCallback(async () => {
    setBusy("pair");
    setFeedback("Pairing... check your TV");
    try {
      const res = await call<[], OkResult>("pair_tv");
      if (res.ok) {
        setPaired(true);
        if (res.mac_address) setMacAddress(res.mac_address);
        showFeedback("Paired successfully");
      } else {
        showFeedback(res.error || "Pairing failed");
      }
    } catch {
      showFeedback("Pairing failed");
    } finally {
      setBusy(null);
    }
  }, [showFeedback]);

  const handleWake = useCallback(async () => {
    setBusy("wake");
    try {
      const res = await call<[], OkResult>("wake_tv");
      showFeedback(res.ok ? "Wake signal sent" : (res.error || "Wake failed"));
    } catch {
      showFeedback("Wake failed");
    } finally {
      setBusy(null);
    }
  }, [showFeedback]);

  const handleOff = useCallback(async () => {
    setBusy("off");
    try {
      const res = await call<[], OkResult>("turn_off_tv");
      showFeedback(res.ok ? "TV turned off" : (res.error || "Turn off failed"));
    } catch {
      showFeedback("Turn off failed");
    } finally {
      setBusy(null);
    }
  }, [showFeedback]);

  return (
    <div>
      {/* Status */}
      <PanelSection>
        <div style={statusBar}>
          <FaCircle style={{
            ...statusDot,
            color: reachable ? "#4caf50" : "#f44336",
          }} />
          <span style={{ fontSize: "13px" }}>
            {reachable ? "TV is reachable" : "TV is unreachable"}
          </span>
        </div>
      </PanelSection>

      {/* Settings */}
      <PanelSection title="Settings">
        <PanelSectionRow>
          <TextField
            label="TV IP Address"
            value={tvIp}
            onChange={(e) => setTvIp(e.target.value)}
          />
        </PanelSectionRow>

        <PanelSectionRow>
          <Field label="HDMI Input">
            <DialogButton
              onClick={cycleHdmi}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "8px 12px",
                minWidth: "120px",
              }}
            >
              {hdmiLabel(hdmiInput)}
              <FaChevronRight style={{ fontSize: "10px", opacity: 0.5 }} />
            </DialogButton>
          </Field>
        </PanelSectionRow>

        <PanelSectionRow>
          <TextField
            label="MAC Address"
            description="Auto-filled on pair, or enter manually"
            value={macAddress}
            onChange={(e) => setMacAddress(e.target.value)}
          />
        </PanelSectionRow>

        <ToggleField
          label="Wake on Guide Button"
          description="Press gamepad Guide/Home to wake TV"
          checked={wakeOnGuide}
          onChange={(val) => setWakeOnGuide(val)}
        />

        <ToggleField
          label="Wake on Resume"
          description="Wake TV when Deck resumes from sleep"
          checked={wakeOnResume}
          onChange={(val) => setWakeOnResume(val)}
        />

        <PanelSectionRow>
          <ButtonItem onClick={handleSave} disabled={busy !== null} layout="below">
            {busy === "save" ? <Spinner width={16} height={16} /> : <FaSave />}
            {" "}Save Settings
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {/* Controls */}
      <PanelSection title="Controls">
        <PanelSectionRow>
          <DialogButton
            onClick={handlePair}
            disabled={busy !== null || !tvIp}
            style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px", width: "100%" }}
          >
            {busy === "pair" ? <Spinner width={16} height={16} /> : <FaPlug />}
            {paired ? "Re-pair TV" : "Pair TV"}
          </DialogButton>
        </PanelSectionRow>

        <PanelSectionRow>
          <DialogButton
            onClick={handleWake}
            disabled={busy !== null || !macAddress}
            style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px", width: "100%" }}
          >
            {busy === "wake" ? <Spinner width={16} height={16} /> : <FaPowerOff />}
            Wake TV
          </DialogButton>
        </PanelSectionRow>

        <PanelSectionRow>
          <DialogButton
            onClick={handleOff}
            disabled={busy !== null || !paired}
            style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px", width: "100%" }}
          >
            {busy === "off" ? <Spinner width={16} height={16} /> : <FaPowerOff />}
            Turn Off TV
          </DialogButton>
        </PanelSectionRow>
      </PanelSection>

      {/* Feedback */}
      {feedback && (
        <PanelSection>
          <div style={feedbackText}>{feedback}</div>
        </PanelSection>
      )}
    </div>
  );
};
