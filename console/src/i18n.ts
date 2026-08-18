import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import zh from "./locales/zh.json";

const resources = {
  zh: {
    translation: zh,
  },
};

i18n.use(initReactI18next).init({
  resources,
  lng: "zh",
  fallbackLng: "zh",
  supportedLngs: ["zh"],
  nonExplicitSupportedLngs: false,
  interpolation: {
    escapeValue: false,
  },
});

export default i18n;
