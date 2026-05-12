import { staticClasses } from "@decky/ui";
import { definePlugin } from "@decky/api";
import { FaTv } from "react-icons/fa";
import { WakeTVPanel } from "./components/WakeTVPanel";

export default definePlugin(() => {
  return {
    name: "Wake TV",
    titleView: (
      <div className={staticClasses.Title} style={{ fontSize: "16px" }}>Wake TV</div>
    ),
    content: <WakeTVPanel />,
    icon: <FaTv />,
    onDismount() {},
  };
});
