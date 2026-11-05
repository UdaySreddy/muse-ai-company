import { useState, useCallback, createContext, useContext } from 'react';
import { en } from './en';

const langs = { en } as const;
export type Lang = keyof typeof langs;
export type { Translations } from './en';

function detectLang(): Lang {
  // English is the only supported locale.
  return 'en';
}

export function useLanguage() {
  const [lang, setLang] = useState<Lang>(detectLang);

  const t = langs[lang];

  const switchLang = useCallback((l: Lang) => {
    setLang(l);
    localStorage.setItem('lang', l);
  }, []);

  return { t, lang, switchLang };
}

// Context-based approach for sharing language state across the app
export const LanguageContext = createContext<ReturnType<typeof useLanguage> | null>(null);

export function useT() {
  const ctx = useContext(LanguageContext);
  if (!ctx) {
    // Fallback: return English translations directly when used outside provider
    return langs['en'];
  }
  return ctx.t;
}
