import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import en from "./locales/en.json";
import ru from "./locales/ru.json";
import zh from "./locales/zh.json";
import ja from "./locales/ja.json";
import ptBR from "./locales/pt-BR.json";
import id from "./locales/id.json";
import vi from "./locales/vi.json";

const resources = {
  en: {
    translation: en,
  },
  ru: {
    translation: ru,
  },
  zh: {
    translation: zh,
  },
  ja: {
    translation: ja,
  },
  "pt-BR": {
    translation: ptBR,
  },
  id: {
    translation: id,
  },
  vi: {
    translation: vi,
  },
};

// Seed the language choice on first boot so the browser locale becomes
// sticky: without this, an empty localStorage lets the server-side
// default ("en") silently override a non-English browser locale on the
// next full page load (e.g. typing a URL directly).
const initialLanguage =
  localStorage.getItem("language") || navigator.language || "en";
if (!localStorage.getItem("language")) {
  try {
    localStorage.setItem("language", initialLanguage);
  } catch {
    // storage unavailable (private mode): fall through
  }
}

i18n.use(initReactI18next).init({
  resources,
  lng: initialLanguage,
  fallbackLng: "en",
  supportedLngs: Object.keys(resources),
  nonExplicitSupportedLngs: true,
  interpolation: {
    escapeValue: false,
  },
});

export default i18n;
