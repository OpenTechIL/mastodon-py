import { createSelector } from '@reduxjs/toolkit';
import { OrderedSet as ImmutableOrderedSet } from 'immutable';

import type { RootState } from 'mastodon/store';

const EMPTY_ORDERED_SET: ImmutableOrderedSet<string> = ImmutableOrderedSet();

export const getStatusList = createSelector(
  [
    (
      state: RootState,
      type: 'favourites' | 'bookmarks' | 'pins' | 'trending',
    ) =>
      (state.status_lists.getIn([type, 'items']) as
        | ImmutableOrderedSet<string>
        | undefined) ?? EMPTY_ORDERED_SET,
  ],
  (items) => items.toList(),
);
