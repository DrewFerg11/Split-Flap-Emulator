/* SPDX-License-Identifier: GPL-2.0-only */
#include "WString.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <cstring>

namespace {

template <typename Unsigned> std::string digits(Unsigned value, unsigned char base) {
    if (base < 2 || base > 36)
        base = 10;
    std::string result;
    do {
        int digit = int(value % base);
        result += char(digit < 10 ? '0' + digit : 'a' + digit - 10);
        value /= base;
    } while (value);
    std::reverse(result.begin(), result.end());
    return result;
}

template <typename Signed> std::string signed_digits(Signed value, unsigned char base) {
    using Unsigned = std::make_unsigned_t<Signed>;
    if (value < 0 && base == 10)
        return "-" + digits(Unsigned(0) - Unsigned(value), base);
    return digits(Unsigned(value), base);
}

/* arduino-esp32 dtostrf(value, decimals + 2, decimals): left-padded to the minimum width and
 * rounded by adding half of the last printed digit before digit-by-digit expansion. */
std::string fixed(double value, unsigned int decimals) {
    if (std::isnan(value))
        return "nan";
    if (std::isinf(value))
        return "inf";
    int fill = int(decimals) + 2 - (decimals ? int(decimals) + 1 : 0);
    bool negative = value < 0.0;
    if (negative) {
        fill--;
        value = -value;
    }
    double half = 0.5;
    for (unsigned int i = 0; i < decimals; i++)
        half /= 10.0;
    value += half;
    double scale = 1.0;
    unsigned int count = 1;
    while (value >= 10.0 * scale) {
        scale *= 10.0;
        count++;
    }
    value /= scale;
    fill -= int(count);
    std::string result(fill > 0 ? size_t(fill) : 0, ' ');
    if (negative)
        result += '-';
    for (count += decimals; count-- > 0;) {
        int digit = std::min(9, int(value));
        result += char('0' + digit);
        if (count == decimals && decimals > 0)
            result += '.';
        value = (value - digit) * 10.0;
    }
    return result;
}

} // namespace

String::String(unsigned char value, unsigned char base) : text(digits(value, base)) {}
String::String(int value, unsigned char base) : text(signed_digits(value, base)) {}
String::String(unsigned int value, unsigned char base) : text(digits(value, base)) {}
String::String(long value, unsigned char base) : text(signed_digits(value, base)) {}
String::String(unsigned long value, unsigned char base) : text(digits(value, base)) {}
String::String(long long value, unsigned char base) : text(signed_digits(value, base)) {}
String::String(unsigned long long value, unsigned char base) : text(digits(value, base)) {}
String::String(float value, unsigned int decimals) : text(fixed(value, decimals)) {}
String::String(double value, unsigned int decimals) : text(fixed(value, decimals)) {}

char &String::operator[](unsigned int index) {
    static char invalid;
    if (index >= text.size()) {
        invalid = 0;
        return invalid;
    }
    return text[index];
}

bool String::equalsIgnoreCase(const String &other) const {
    return text.size() == other.text.size() &&
           std::equal(text.begin(), text.end(), other.text.begin(), [](char a, char b) {
               return std::tolower(static_cast<unsigned char>(a)) ==
                      std::tolower(static_cast<unsigned char>(b));
           });
}

bool String::startsWith(const String &prefix, unsigned int offset) const {
    if (offset > text.size() || prefix.text.size() > text.size() - offset)
        return false;
    return text.compare(offset, prefix.text.size(), prefix.text) == 0;
}

bool String::endsWith(const String &suffix) const {
    return suffix.text.size() <= text.size() &&
           text.compare(text.size() - suffix.text.size(), suffix.text.size(), suffix.text) == 0;
}

int String::indexOf(char value, unsigned int from) const {
    if (from >= text.size())
        return -1;
    size_t found = text.find(value, from);
    return found == std::string::npos ? -1 : int(found);
}

int String::indexOf(const String &value, unsigned int from) const {
    if (from >= text.size())
        return -1;
    size_t found = text.find(value.text, from);
    return found == std::string::npos ? -1 : int(found);
}

int String::lastIndexOf(char value) const {
    return text.empty() ? -1 : lastIndexOf(value, length() - 1);
}

int String::lastIndexOf(char value, unsigned int from) const {
    if (from >= text.size())
        return -1;
    size_t found = text.rfind(value, from);
    return found == std::string::npos ? -1 : int(found);
}

int String::lastIndexOf(const String &value) const {
    return text.empty() ? -1 : lastIndexOf(value, length() - 1);
}

int String::lastIndexOf(const String &value, unsigned int from) const {
    if (value.text.empty() || text.empty() || value.text.size() > text.size())
        return -1;
    from = std::min<unsigned int>(from, length() - 1);
    size_t found = text.rfind(value.text, from);
    return found == std::string::npos ? -1 : int(found);
}

String String::substring(unsigned int from, unsigned int to) const {
    if (from > to)
        std::swap(from, to);
    if (from >= text.size())
        return String();
    to = std::min<unsigned int>(to, length());
    return String(text.data() + from, to - from);
}

void String::replace(char find, char with) { std::replace(text.begin(), text.end(), find, with); }

void String::replace(const String &find, const String &with) {
    if (text.empty() || find.text.empty())
        return;
    std::string result;
    size_t start = 0;
    for (size_t found; (found = text.find(find.text, start)) != std::string::npos;
         start = found + find.text.size())
        result.append(text, start, found - start).append(with.text);
    text = result.append(text, start, std::string::npos);
}

void String::remove(unsigned int index, unsigned int count) {
    if (index >= text.size())
        return;
    text.erase(index, std::min<size_t>(count, text.size() - index));
}

void String::toLowerCase() {
    for (char &c : text)
        c = char(std::tolower(static_cast<unsigned char>(c)));
}

void String::toUpperCase() {
    for (char &c : text)
        c = char(std::toupper(static_cast<unsigned char>(c)));
}

void String::trim() {
    auto space = [](char c) { return std::isspace(static_cast<unsigned char>(c)) != 0; };
    text.erase(text.begin(), std::find_if_not(text.begin(), text.end(), space));
    text.erase(std::find_if_not(text.rbegin(), text.rend(), space).base(), text.end());
}

long String::toInt() const { return std::atol(text.c_str()); }
float String::toFloat() const { return float(std::atof(text.c_str())); }
double String::toDouble() const { return std::atof(text.c_str()); }

void String::getBytes(unsigned char *buffer, unsigned int size, unsigned int index) const {
    if (!size || !buffer)
        return;
    if (index >= text.size()) {
        buffer[0] = 0;
        return;
    }
    size_t count = std::min<size_t>(size - 1, text.size() - index);
    std::memcpy(buffer, text.data() + index, count);
    buffer[count] = 0;
}

String operator+(const String &left, const String &right) {
    String result(left);
    result.concat(right);
    return result;
}
String operator+(const String &left, const char *right) {
    String result(left);
    result.concat(right);
    return result;
}
String operator+(const char *left, const String &right) {
    String result(left);
    result.concat(right);
    return result;
}
String operator+(const String &left, char right) {
    String result(left);
    result.concat(right);
    return result;
}
String operator+(char left, const String &right) {
    String result(left);
    result.concat(right);
    return result;
}
