import { Layout } from "antd";
import styles from "./index.module.less";
import { Slot } from "../plugins/registry/Slot";

const { Header: AntHeader } = Layout;

/**
 * Offline deployments deliberately expose only local controls in the header.
 * Update checks, documentation and source-code links would either fail on an
 * intranet or send users to services that are not part of this product.
 */
export default function Header() {
  return (
    <AntHeader className={styles.header}>
      <div className={styles.logoWrapper}>
        <Slot name="header.logo" kind="replace">
          <span className={styles.brandMark} aria-label="汇智小助手">
            <img src="/huizhi-favicon.svg" alt="" className={styles.logoImg} />
            <span className={styles.brandName}>汇智小助手</span>
          </span>
        </Slot>
      </div>
      <Slot name="header.left" kind="fill" />
      <Slot name="header.right" kind="fill" />
    </AntHeader>
  );
}
