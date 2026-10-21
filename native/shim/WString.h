/* SPDX-License-Identifier: GPL-2.0-only */
/* Arduino String with the arduino-esp32 2.x API surface, backed by std::string. */
#pragma once
#include <cstddef>
#include <cstdint>
#include <string>
#include <type_traits>

class __FlashStringHelper;
#define F(string_literal) (reinterpret_cast<const __FlashStringHelper *>(string_literal))

class String {
  public:
    String() = default;
    String(const char *value) : text(value ? value : "") {}
    String(const char *value, unsigned int length) : text(value ? value : "", value ? length : 0) {}
    String(const __FlashStringHelper *value) : String(reinterpret_cast<const char *>(value)) {}
    String(const String &) = default;
    String(String &&) noexcept = default;
    explicit String(char value) : text(1, value) {}
    explicit String(unsigned char value, unsigned char base = 10);
    explicit String(int value, unsigned char base = 10);
    explicit String(unsigned int value, unsigned char base = 10);
    explicit String(long value, unsigned char base = 10);
    explicit String(unsigned long value, unsigned char base = 10);
    explicit String(long long value, unsigned char base = 10);
    explicit String(unsigned long long value, unsigned char base = 10);
    explicit String(float value, unsigned int decimals = 2);
    explicit String(double value, unsigned int decimals = 2);

    String &operator=(const String &) = default;
    String &operator=(String &&) noexcept = default;
    String &operator=(const char *value) {
        text = value ? value : "";
        return *this;
    }

    unsigned int length() const { return static_cast<unsigned int>(text.size()); }
    bool isEmpty() const { return text.empty(); }
    const char *c_str() const { return text.c_str(); }
    char *begin() { return text.data(); }
    char *end() { return text.data() + text.size(); }
    const char *begin() const { return text.data(); }
    const char *end() const { return text.data() + text.size(); }
    bool reserve(unsigned int size) {
        text.reserve(size);
        return true;
    }

    bool concat(const String &value) {
        text += value.text;
        return true;
    }
    bool concat(const char *value) {
        if (!value)
            return false;
        text += value;
        return true;
    }
    bool concat(const char *value, unsigned int length) {
        if (!value)
            return false;
        text.append(value, length);
        return true;
    }
    bool concat(char value) {
        text += value;
        return true;
    }
    template <typename T, typename = std::enable_if_t<std::is_arithmetic_v<T>>>
    bool concat(T value) {
        return concat(String(value));
    }
    template <typename T> String &operator+=(const T &value) {
        concat(value);
        return *this;
    }

    char charAt(unsigned int index) const { return index < text.size() ? text[index] : 0; }
    void setCharAt(unsigned int index, char value) {
        if (index < text.size())
            text[index] = value;
    }
    char operator[](unsigned int index) const { return charAt(index); }
    char &operator[](unsigned int index);

    int compareTo(const String &other) const { return text.compare(other.text); }
    bool equals(const String &other) const { return text == other.text; }
    bool equals(const char *other) const { return text == (other ? other : ""); }
    bool equalsIgnoreCase(const String &other) const;
    bool startsWith(const String &prefix, unsigned int offset = 0) const;
    bool endsWith(const String &suffix) const;
    bool operator==(const String &other) const { return equals(other); }
    bool operator==(const char *other) const { return equals(other); }
    bool operator!=(const String &other) const { return !equals(other); }
    bool operator!=(const char *other) const { return !equals(other); }
    bool operator<(const String &other) const { return text < other.text; }
    bool operator>(const String &other) const { return text > other.text; }
    bool operator<=(const String &other) const { return text <= other.text; }
    bool operator>=(const String &other) const { return text >= other.text; }

    int indexOf(char value, unsigned int from = 0) const;
    int indexOf(const String &value, unsigned int from = 0) const;
    int lastIndexOf(char value) const;
    int lastIndexOf(char value, unsigned int from) const;
    int lastIndexOf(const String &value) const;
    int lastIndexOf(const String &value, unsigned int from) const;
    String substring(unsigned int from) const { return substring(from, length()); }
    String substring(unsigned int from, unsigned int to) const;

    void replace(char find, char with);
    void replace(const String &find, const String &with);
    void remove(unsigned int index) { remove(index, ~0u); }
    void remove(unsigned int index, unsigned int count);
    void toLowerCase();
    void toUpperCase();
    void trim();

    long toInt() const;
    float toFloat() const;
    double toDouble() const;
    void getBytes(unsigned char *buffer, unsigned int size, unsigned int index = 0) const;
    void toCharArray(char *buffer, unsigned int size, unsigned int index = 0) const {
        getBytes(reinterpret_cast<unsigned char *>(buffer), size, index);
    }

  private:
    std::string text;
};

String operator+(const String &left, const String &right);
String operator+(const String &left, const char *right);
String operator+(const char *left, const String &right);
String operator+(const String &left, char right);
String operator+(char left, const String &right);
template <typename T,
          typename = std::enable_if_t<std::is_arithmetic_v<T> && !std::is_same_v<T, char>>>
String operator+(const String &left, T right) {
    return left + String(right);
}
template <typename T,
          typename = std::enable_if_t<std::is_arithmetic_v<T> && !std::is_same_v<T, char>>>
String operator+(T left, const String &right) {
    return String(left) + right;
}
inline bool operator==(const char *left, const String &right) { return right == left; }
inline bool operator!=(const char *left, const String &right) { return right != left; }
