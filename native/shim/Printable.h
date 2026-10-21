/* SPDX-License-Identifier: GPL-2.0-only */
#pragma once
#include <cstddef>

class Print;

class Printable {
  public:
    virtual ~Printable() = default;
    virtual size_t printTo(Print &printer) const = 0;
};
