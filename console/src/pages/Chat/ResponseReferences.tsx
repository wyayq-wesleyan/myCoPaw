import { useState } from "react";
import { useTranslation } from "react-i18next";

import { visibleReferences, type ChatReference } from "./references";
import styles from "./ResponseReferences.module.less";

interface ResponseReferencesProps {
  references: ChatReference[];
}

/**
 * The "参考资料" footer under an assistant answer.
 *
 * It stays intentionally quiet: a hairline separator, a small gray title and
 * numbered filenames. Three are visible by default; the rest are one click
 * away. The list is built from structured tool metadata (see references.ts),
 * so it covers every document/table the turn actually queried, not only the
 * five filenames embedded in the tool's human-readable text.
 */
export function ResponseReferences({ references }: ResponseReferencesProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);

  if (references.length === 0) {
    return null;
  }

  const visible = visibleReferences(references, expanded);
  const hiddenCount = references.length - visibleReferences(references, false).length;

  return (
    <section
      className={styles.references}
      aria-label={t("chat.references.title")}
    >
      <div className={styles.divider} />
      <div className={styles.title}>{t("chat.references.title")}</div>
      <ol className={styles.list}>
        {visible.map((reference, index) => (
          <li
            className={styles.item}
            key={`${reference.kind}:${reference.id || reference.name}:${index}`}
          >
            <span className={styles.index}>{index + 1}.</span>
            <span className={styles.name}>{reference.name}</span>
          </li>
        ))}
      </ol>
      {hiddenCount > 0 ? (
        <button
          className={styles.toggle}
          type="button"
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded
            ? t("chat.references.less")
            : t("chat.references.more", { count: hiddenCount })}
        </button>
      ) : null}
    </section>
  );
}

export default ResponseReferences;
