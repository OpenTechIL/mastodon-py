import { useCallback } from 'react';

import { defineMessages, useIntl } from 'react-intl';

import { changeSetting } from 'mastodon/actions/settings';
import {
  selectAppearanceColorScheme,
  selectAppearanceHighContrast,
  selectAppearanceReduceMotion,
} from 'mastodon/selectors/settings';
import { useAppDispatch, useAppSelector } from 'mastodon/store';

const messages = defineMessages({
  dark: {
    id: 'appearance.theme.dark',
    defaultMessage: 'Dark theme',
  },
  light: {
    id: 'appearance.theme.light',
    defaultMessage: 'Light theme',
  },
  auto: {
    id: 'appearance.theme.auto',
    defaultMessage: 'Auto theme (follows system)',
  },
  highContrast: {
    id: 'appearance.high_contrast',
    defaultMessage: 'High contrast',
  },
  reduceMotion: {
    id: 'appearance.reduce_motion',
    defaultMessage: 'Reduce motion',
  },
});

type ColorScheme = 'dark' | 'auto' | 'light';

/** Apply a color-scheme preference immediately to the document element. */
function applyColorScheme(pref: ColorScheme) {
  const el = document.documentElement;
  if (pref === 'auto') {
    const prefersDark = window.matchMedia(
      '(prefers-color-scheme: dark)',
    ).matches;
    el.dataset.colorScheme = prefersDark ? 'dark' : 'light';
  } else {
    el.dataset.colorScheme = pref;
  }
}

function applyContrast(high: boolean) {
  document.documentElement.dataset.contrast = high ? 'high' : 'default';
}

// SVG icons (Material Design 24px paths) inlined to avoid adding extra SVG files.
const SunIcon = () => (
  <svg
    viewBox='0 0 24 24'
    fill='currentColor'
    width='18'
    height='18'
    aria-hidden='true'
  >
    <path d='M12 7a5 5 0 1 0 0 10A5 5 0 0 0 12 7zm0-5a1 1 0 0 1 1 1v2a1 1 0 0 1-2 0V3a1 1 0 0 1 1-1zm0 16a1 1 0 0 1 1 1v2a1 1 0 0 1-2 0v-2a1 1 0 0 1 1-1zM3 11h2a1 1 0 0 1 0 2H3a1 1 0 0 1 0-2zm16 0h2a1 1 0 0 1 0 2h-2a1 1 0 0 1 0-2zM5.636 4.222a1 1 0 0 1 1.414 1.414L5.636 7.05a1 1 0 0 1-1.414-1.414l1.414-1.414zm12.728 12.728a1 1 0 0 1 1.414 1.414l-1.414 1.414a1 1 0 0 1-1.414-1.414l1.414-1.414zM4.222 18.364a1 1 0 0 1 1.414-1.414l1.414 1.414a1 1 0 0 1-1.414 1.414L4.222 18.364zM17 5.636a1 1 0 0 1 1.414-1.414l1.414 1.414a1 1 0 0 1-1.414 1.414L17 5.636z' />
  </svg>
);

const MoonIcon = () => (
  <svg
    viewBox='0 0 24 24'
    fill='currentColor'
    width='18'
    height='18'
    aria-hidden='true'
  >
    <path d='M12 3a9 9 0 1 0 9 9c0-.46-.04-.92-.1-1.36a5.389 5.389 0 0 1-4.4 2.26 5.403 5.403 0 0 1-3.14-9.8c-.44-.06-.9-.1-1.36-.1z' />
  </svg>
);

const AutoIcon = () => (
  <svg
    viewBox='0 0 24 24'
    fill='currentColor'
    width='18'
    height='18'
    aria-hidden='true'
  >
    <path d='M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18V4a8 8 0 0 1 0 16z' />
  </svg>
);

const ContrastIcon = () => (
  <svg
    viewBox='0 0 24 24'
    fill='currentColor'
    width='18'
    height='18'
    aria-hidden='true'
  >
    <path d='M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18V4c4.41 0 8 3.59 8 8s-3.59 8-8 8z' />
  </svg>
);

const MotionIcon = () => (
  <svg
    viewBox='0 0 24 24'
    fill='currentColor'
    width='18'
    height='18'
    aria-hidden='true'
  >
    <path d='M13.49 5.48c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm-3.6 13.9l1-4.4 2.1 2v6h2v-7.5l-2.1-2 .6-3c1.3 1.5 3.3 2.5 5.5 2.5v-2c-1.9 0-3.5-1-4.3-2.4l-1-1.6c-.4-.6-1-1-1.7-1-.3 0-.5.1-.8.1l-5.2 2.2v4.7h2v-3.4l1.8-.7-1.6 8.1-4.9-1-.4 2 7 1.4z' />
  </svg>
);

const CYCLE: ColorScheme[] = ['dark', 'auto', 'light'];

/** Compact single-button theme cycler for use in column headers. */
export const ThemeCycleButton: React.FC = () => {
  const intl = useIntl();
  const dispatch = useAppDispatch();
  const colorScheme = useAppSelector(selectAppearanceColorScheme);

  const handleClick = useCallback(() => {
    const next =
      CYCLE[(CYCLE.indexOf(colorScheme) + 1) % CYCLE.length] ?? 'auto';
    applyColorScheme(next);
    dispatch(changeSetting(['appearance', 'colorScheme'], next));
  }, [dispatch, colorScheme]);

  const label =
    colorScheme === 'dark'
      ? intl.formatMessage(messages.dark)
      : colorScheme === 'light'
        ? intl.formatMessage(messages.light)
        : intl.formatMessage(messages.auto);

  return (
    <button
      type='button'
      className='column-header__button'
      onClick={handleClick}
      title={label}
      aria-label={label}
    >
      {colorScheme === 'dark' ? (
        <MoonIcon />
      ) : colorScheme === 'light' ? (
        <SunIcon />
      ) : (
        <AutoIcon />
      )}
    </button>
  );
};

export const ThemeToggle: React.FC = () => {
  const intl = useIntl();
  const dispatch = useAppDispatch();

  const colorScheme = useAppSelector(selectAppearanceColorScheme);
  const highContrast = useAppSelector(selectAppearanceHighContrast);
  const reduceMotion = useAppSelector(selectAppearanceReduceMotion);

  const handleThemeCycle = useCallback(() => {
    const next =
      CYCLE[(CYCLE.indexOf(colorScheme) + 1) % CYCLE.length] ?? 'auto';
    applyColorScheme(next);
    dispatch(changeSetting(['appearance', 'colorScheme'], next));
  }, [dispatch, colorScheme]);

  const handleContrastToggle = useCallback(() => {
    const next = !highContrast;
    applyContrast(next);
    dispatch(changeSetting(['appearance', 'highContrast'], next));
  }, [dispatch, highContrast]);

  const handleMotionToggle = useCallback(() => {
    const next = !reduceMotion;
    dispatch(changeSetting(['appearance', 'reduceMotion'], next));
    // Mastodon's CSS checks .reduce-motion on <body>
    document.body.classList.toggle('reduce-motion', next);
  }, [dispatch, reduceMotion]);

  const themeLabel =
    colorScheme === 'dark'
      ? intl.formatMessage(messages.dark)
      : colorScheme === 'light'
        ? intl.formatMessage(messages.light)
        : intl.formatMessage(messages.auto);

  return (
    <div
      className='navigation-panel__appearance'
      role='toolbar'
      aria-label='Appearance'
    >
      <button
        type='button'
        className={`appearance-toggle ${colorScheme}`}
        onClick={handleThemeCycle}
        title={themeLabel}
        aria-label={themeLabel}
        aria-pressed={colorScheme !== 'auto'}
      >
        {colorScheme === 'dark' ? (
          <MoonIcon />
        ) : colorScheme === 'light' ? (
          <SunIcon />
        ) : (
          <AutoIcon />
        )}
        <span className='appearance-toggle__label'>
          {colorScheme === 'dark'
            ? 'Dark'
            : colorScheme === 'light'
              ? 'Light'
              : 'Auto'}
        </span>
      </button>

      <button
        type='button'
        className={`appearance-toggle ${highContrast ? 'active' : ''}`}
        onClick={handleContrastToggle}
        title={intl.formatMessage(messages.highContrast)}
        aria-label={intl.formatMessage(messages.highContrast)}
        aria-pressed={highContrast}
      >
        <ContrastIcon />
      </button>

      <button
        type='button'
        className={`appearance-toggle ${reduceMotion ? 'active' : ''}`}
        onClick={handleMotionToggle}
        title={intl.formatMessage(messages.reduceMotion)}
        aria-label={intl.formatMessage(messages.reduceMotion)}
        aria-pressed={reduceMotion}
      >
        <MotionIcon />
      </button>
    </div>
  );
};
